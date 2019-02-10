from __future__ import print_function

import argparse
import collections
import lark
import os
import re
import sys


def namedtuple_with_defaults(typename, field_names, default_values=()):
  ntuple = collections.namedtuple(typename, field_names)
  ntuple.__new__.__defaults__ = (None,) * len(ntuple._fields)
  if isinstance(default_values, collections.Mapping):
    prototype = ntuple(**default_values)
  else:
    prototype = ntuple(*default_values)
  ntuple.__new__.__defaults__ = tuple(prototype)
  return ntuple


FuncGen = namedtuple_with_defaults(
    'FuncGen',
    'tree, xtree, rwxtree, func, xfunc, code, sig, rwsig, cppsig, funsig, mapsig'
)

FuncOpts = namedtuple_with_defaults('FuncOpts', 'ref_param')

_GRAMMAR = r"""
    start: type fnname "(" params ")"
    type: CONST? core_type refspec?
    fnname: CNAME
    refspec: REF
           | PTR
    core_type: template
        | TNAME
    template: TNAME "<" typelist ">"
    typelist: type
            | type "," typelist
    REF: "&"
    PTR: "*"
    CONST: "const"
    TNAME: /[a-zA-Z0-9_:]+/
    HEXNUMBER: /0x[0-9a-fA-F]+/
    params: param
          | param "," params
    param: type param_name param_defval?
    param_name: CNAME

    param_defval: "=" init_value
    init_value: "true"
              | "false"
              | "{}"
              | NUMBER
              | SIGNED_NUMBER
              | HEXNUMBER
              | ESCAPED_STRING

    %import common.CNAME -> CNAME
    %import common.NUMBER -> NUMBER
    %import common.SIGNED_NUMBER -> SIGNED_NUMBER
    %import common.ESCAPED_STRING -> ESCAPED_STRING
    %import common.WS
    %ignore WS
    """

_PARSER = lark.Lark(_GRAMMAR, parser='lalr', propagate_positions=True)

_XPARSER = lark.Lark(
    _GRAMMAR, parser='lalr', propagate_positions=True, keep_all_tokens=True)

_FN_BLACKLIST = set([
    # ATEN functions
    'toBackend',
    'toScalarType',
    'backward',
    'set_data',
    'tensorFromBlob',
    'tensorWithAllocator',
    'storageFromBlob',
    'storageWithAllocator',
    'unsafeStorageFromTH',
    'unsafeTensorFromTH',
    # XLA/TPU functions
    'ones_like',
    'zeros_like',
])

_FN_BLACKLIST_REGEX = [
    # ATEN functions
    r'[^(]*cudnn',
    # XLA/TPU functions
]

_TYPE_NSMAP = {
    'Tensor': 'at::Tensor',
    'TensorList': 'at::TensorList',
    'Scalar': 'at::Scalar',
    'Storage': 'at::Storage',
    'IntList': 'at::IntList',
    'IntArrayRef': 'at::IntArrayRef',
    'Generator': 'at::Generator',
    'ScalarType': 'at::ScalarType',
    'TensorOptions': 'at::TensorOptions',
    'SparseTensorRef': 'at::SparseTensorRef',
    'Device': 'c10::Device',
    'optional': 'at::optional',
}

_CPP_HEADER = """// Autogenerated file by {gen}. Do not edit directly!

#include "aten_xla_bridge.h"
#include <ATen/ExtensionBackendRegistration.h>

namespace torch_xla {{
namespace {{

{funcs}
}}  // namespace

{regs}
}}  // namespace torch_xla
"""

_H_CLASS_HEADER = """// Autogenerated file by {gen}. Do not edit directly!
#pragma once

#include <ATen/TypeDefault.h>

namespace torch_xla {{

class AtenXlaTypeBase : public at::TypeDefault {{
 public:
  AtenXlaTypeBase(at::TensorTypeId type_id, bool is_variable, bool is_undefined);

  caffe2::TypeMeta typeMeta() const override;

  at::Backend backend() const override;

  at::Allocator* allocator() const override;

  c10::Device getDeviceFromPtr(void* data) const override;

  std::unique_ptr<at::Generator> generator() const override;

  at::TypeID ID() const override;

{hfuncs}
}};

}}  // namespace torch_xla
"""

_CPP_CLASS_HEADER = """// Autogenerated file by {gen}. Do not edit directly!
#include "aten_xla_type_base.h"

#include <ATen/Context.h>
#include <ATen/CPUGenerator.h>
#include <ATen/TypeDefault.h>

#include "aten_xla_bridge.h"
#include "tensorflow/compiler/xla/xla_client/debug_macros.h"
#include "tensorflow/compiler/xla/xla_client/metrics.h"

namespace torch_xla {{

AtenXlaTypeBase::AtenXlaTypeBase(at::TensorTypeId type_id, bool is_variable, bool is_undefined)
    : at::TypeDefault(type_id, is_variable, is_undefined) {{}}

caffe2::TypeMeta AtenXlaTypeBase::typeMeta() const {{
  return scalarTypeToTypeMeta(scalarType());
}}

at::Backend AtenXlaTypeBase::backend() const {{
  return {backend};
}}

at::Allocator* AtenXlaTypeBase::allocator() const {{
  return at::getCPUAllocator();
}}

c10::Device AtenXlaTypeBase::getDeviceFromPtr(void* data) const {{
  return {device_type};
}}

std::unique_ptr<at::Generator> AtenXlaTypeBase::generator() const {{
  return std::unique_ptr<at::Generator>(new at::CPUGenerator(&at::globalContext()));
}}

at::TypeID AtenXlaTypeBase::ID() const {{
  return {typeid};
}}

{funcs}
}}  // namespace torch_xla
"""

_CLASS_INST_HEADER = """
class {type_name} : public AtenXlaType {{
 public:
  {type_name}(at::TensorTypeId type_id, bool is_variable, bool is_undefined)
    : AtenXlaType(type_id, is_variable, is_undefined) {{}}

  at::ScalarType scalarType() const override {{
    return {scalar_type};
  }}

  const char* toString() const override {{
    return "{type_name}";
  }}

  size_t elementSizeInBytes() const override {{
    return {sizeof};
  }}
}};

static inline at::Type* Get{type_name}() {{
  static {type_name}* xla_type = new {type_name}(
    {tensorid}, /*is_variable=*/false, /*is_undefined=*/false);
  return xla_type;
}}

"""

_CLASS_INSTANCES_HEADER = """// Autogenerated file by {gen}. Do not edit directly!
#include "aten_xla_type.h"

#include <ATen/Context.h>

namespace torch_xla {{

{instances}
static inline void RegisterAtenXlaTypes() {{
  auto& context = at::globalContext();
  context.registerType(at::Backend::XLA, at::ScalarType::Byte, GetXLATypeByte());
  context.registerType(at::Backend::XLA, at::ScalarType::Char, GetXLATypeChar());
  context.registerType(at::Backend::XLA, at::ScalarType::Short, GetXLATypeShort());
  context.registerType(at::Backend::XLA, at::ScalarType::Int, GetXLATypeInt());
  context.registerType(at::Backend::XLA, at::ScalarType::Long, GetXLATypeLong());
  context.registerType(at::Backend::XLA, at::ScalarType::Float, GetXLATypeFloat());
}}

}}  // namespace torch_xla
"""

_XLA_FUNCTIONS = {}

_CTOR_FUNCTIONS = {
    'empty': '.device(at::DeviceType::CPU)',
    'linspace': '.device(at::DeviceType::CPU)',
    'logspace': '.device(at::DeviceType::CPU)',
    'ones': '.device(at::DeviceType::CPU)',
    'randn': '.device(at::DeviceType::CPU)',
    'zeros': '.device(at::DeviceType::CPU)',
}

_FUNCTION_OPTIONS = {
    'to(Tensor, TensorOptions, bool, bool) -> Tensor':
        FuncOpts(ref_param='options'),
    'to(Tensor, Device, ScalarType, bool, bool) -> Tensor':
        FuncOpts(ref_param='device'),
    'to(Tensor, Tensor, bool, bool) -> Tensor':
        FuncOpts(ref_param='other'),
}

_RESULT_NAME = 'x_result'


class Context(object):

  def __init__(self, functions, native_functions, gen_class_mode):
    self.gen_class_mode = gen_class_mode
    self.defdb = {}
    with open(functions, 'r') as ff:
      self.functions_data = ff.read()
    with open(native_functions, 'r') as ff:
      self.native_functions_data = ff.read()

  def get_function(self, name, ref_param):
    if self.functions_data.find(' {}('.format(name)) >= 0:
      return 'at::{}'.format(name)
    if self.native_functions_data.find(' {}('.format(name)) >= 0:
      return 'at::native::{}'.format(name)
    return 'at::detail::infer_type({}).{}'.format(ref_param, name)


class StringEmit(object):

  def __init__(self, sref):
    self.sref = sref
    self.sval = ''
    self.pos = -1

  def __repr__(self):
    return self.sval

  def advance(self, t):
    start = t.column - 1
    end = t.end_column - 1
    pos = self.pos if self.pos >= 0 else start
    if start > pos:
      self.sval += self.sref[pos:start]
    self.sval += t.value
    self.pos = end

  def skip(self, t):
    self.pos = last_match(t) if self.pos >= 0 else -1

  def append(self, s):
    self.sval += s
    self.pos = -1


class TensorFetcher(object):

  def __init__(self, var_name):
    self.var_name = var_name
    self.tensors = []
    self.writeable = []

  def add(self, name, writeable):
    self.tensors.append(name)
    self.writeable.append('true' if writeable else 'false')
    return '{}[{}]'.format(self.var_name, len(self.tensors) - 1)

  def generate(self):
    tvar_name = '{}_tensors'.format(self.var_name)
    wvar_name = '{}_writeables'.format(self.var_name)
    code = ''
    code += '  std::vector<at::Tensor> {} = {{{}}};\n'.format(
        tvar_name, ', '.join(self.tensors))
    code += '  std::vector<bool> {} = {{{}}};\n'.format(
        wvar_name, ', '.join(self.writeable))
    code += ('  auto {} = bridge::XlaCreateTensorList({}, &{});\n').format(
        self.var_name, tvar_name, wvar_name)
    return code


def list_get(l, n):
  return l[n] if n < len(l) else None


def is_blacklisted_fn(fname, mapsig):
  if fname in _FN_BLACKLIST or mapsig in _FN_BLACKLIST:
    return True
  for frx in _FN_BLACKLIST_REGEX:
    if re.match(frx, fname) or re.match(frx, mapsig):
      return True
  return False


def create_type_instances():
  code = ''
  code += _CLASS_INST_HEADER.format(
      type_name='XLATypeByte',
      scalar_type='at::ScalarType::Byte',
      sizeof=1,
      tensorid='c10::XLATensorId()')
  code += _CLASS_INST_HEADER.format(
      type_name='XLATypeChar',
      scalar_type='at::ScalarType::Char',
      sizeof=1,
      tensorid='c10::XLATensorId()')
  code += _CLASS_INST_HEADER.format(
      type_name='XLATypeShort',
      scalar_type='at::ScalarType::Short',
      sizeof=2,
      tensorid='c10::XLATensorId()')
  code += _CLASS_INST_HEADER.format(
      type_name='XLATypeInt',
      scalar_type='at::ScalarType::Int',
      sizeof=4,
      tensorid='c10::XLATensorId()')
  code += _CLASS_INST_HEADER.format(
      type_name='XLATypeLong',
      scalar_type='at::ScalarType::Long',
      sizeof=8,
      tensorid='c10::XLATensorId()')
  code += _CLASS_INST_HEADER.format(
      type_name='XLATypeFloat',
      scalar_type='at::ScalarType::Float',
      sizeof=4,
      tensorid='c10::XLATensorId()')
  return code


def first_match(t):
  if isinstance(t, lark.lexer.Token):
    return t.column - 1
  assert isinstance(t, lark.tree.Tree)
  return first_match(t.children[0])


def last_match(t):
  if isinstance(t, lark.lexer.Token):
    return t.end_column - 1
  assert isinstance(t, lark.tree.Tree)
  return last_match(t.children[-1])


def for_every_token(t, fn):
  if isinstance(t, lark.lexer.Token):
    fn(t)
  else:
    assert isinstance(t, lark.tree.Tree)
    for c in t.children:
      for_every_token(c, fn)


def emit_string(t, emit, emit_fn):
  status = emit_fn(t)
  if status > 0:

    def do_emit(tok):
      emit.advance(tok)

    for_every_token(t, do_emit)
  elif status == 0:
    if isinstance(t, lark.lexer.Token):
      emit.advance(t)
    else:
      assert isinstance(t, lark.tree.Tree)
      for c in t.children:
        emit_string(c, emit, emit_fn)
  else:
    emit.skip(t)


def typed_child(t, n, ttype):
  assert isinstance(t, lark.tree.Tree)
  assert n < len(t.children)
  c = t.children[n]
  assert isinstance(c, lark.tree.Tree)
  assert c.data == ttype, t.pretty()
  return c


def rewrite_sig(tree, orig_sig, emit_fn=lambda x: 0):
  emit = StringEmit(orig_sig)
  emit_string(tree, emit, emit_fn)
  return str(emit)


def rewrite_signature(sig, tmap):

  def rewrite(t):
    if t.type == 'TNAME':
      new_type = tmap.get(t.value, None)
      if new_type is not None:
        t.value = new_type

  def emit_fn(t):
    if isinstance(t, lark.lexer.Token):
      return 0
    return -1 if t.data == 'param_defval' else 0

  xtree = _XPARSER.parse(sig)
  for_every_token(xtree, rewrite)
  return rewrite_sig(xtree, sig, emit_fn=emit_fn)


def create_stdfunc_sig(tree, orig_sig):

  def emit_fn(t):
    if isinstance(t, lark.lexer.Token):
      return 0
    return -1 if t.data == 'param_name' else 0

  emit = StringEmit(orig_sig)
  # Emit full function return type.
  emit_string(typed_child(tree, 0, 'type'), emit, emit_fn)
  emit.append('(')
  # Emit parameter list w/out parameter names.
  emit_string(typed_child(tree, 3, 'params'), emit, emit_fn)
  emit.append(')')
  return str(emit)


def create_map_sig(tree, orig_sig):

  def emit_fn(t):
    if isinstance(t, lark.lexer.Token):
      return -1 if t.type in ['CONST', 'REF', 'PTR'] else 0
    return -1 if t.data == 'param_name' else 0

  emit = StringEmit(orig_sig)
  # Emit full function return type.
  emit_string(typed_child(tree, 1, 'fnname'), emit, emit_fn)
  emit.append('(')
  # Emit parameter list w/out parameter names.
  emit_string(typed_child(tree, 3, 'params'), emit, emit_fn)
  emit.append(') -> ')
  emit_string(typed_child(tree, 0, 'type'), emit, emit_fn)
  return str(emit)


def type_core(t):
  assert isinstance(t, lark.tree.Tree)
  for c in t.children:
    if isinstance(c, lark.tree.Tree) and c.data == 'core_type':
      c = c.children[0]
      if isinstance(c, lark.lexer.Token):
        return c.value
      assert isinstance(c, lark.tree.Tree) and c.data == 'template'
      return c.children[0].value
  raise RuntimeError('Not a type tree: {}'.format(t))


def type_is_const(t):
  assert isinstance(t, lark.tree.Tree)
  c = t.children[0]
  return isinstance(c, lark.lexer.Token) and c.value == 'const'


def type_is_refptr(t, kind):
  assert isinstance(t, lark.tree.Tree)
  c = t.children[-1]
  if not isinstance(c, lark.tree.Tree) or c.data != 'refspec':
    return False
  c = c.children[0]
  return isinstance(c, lark.lexer.Token) and c.value == kind


def extract_list(t, l):
  assert isinstance(t, lark.tree.Tree)
  l.append(t.children[0])
  if len(t.children) == 2:
    c = t.children[1]
    if isinstance(c, lark.tree.Tree) and c.data == t.data:
      extract_list(c, l)
  return l


def tuple_type_list(t):
  assert isinstance(t, lark.tree.Tree)
  c = t.children[0]
  assert isinstance(c, lark.tree.Tree) and c.data == 'core_type'
  c = c.children[0]
  assert isinstance(c, lark.tree.Tree) and c.data == 'template'
  types = []
  return extract_list(c.children[1], types)


def get_function_name(t):
  assert isinstance(t, lark.tree.Tree)
  fname = t.children[1]
  assert isinstance(fname, lark.tree.Tree)
  assert fname.data == 'fnname'
  return fname.children[0].value


def get_function_signature(t, orig_sig, namefn):
  emit = StringEmit(orig_sig)
  # Emit full function return type.
  emit_string(typed_child(t, 0, 'type'), emit, lambda t: 0)
  fnname = typed_child(t, 1, 'fnname').children[0]
  xfname = namefn(fnname.value)
  emit.append(' {}('.format(xfname))
  # Emit parameter list w/out parameter names.
  emit_string(typed_child(t, 3, 'params'), emit, lambda t: 0)
  emit.append(')')
  return str(emit), fnname.value, xfname


def get_parameters(t):
  assert isinstance(t, lark.tree.Tree)
  c = t.children[2]
  assert isinstance(c, lark.tree.Tree)
  assert c.data == 'params'
  params = []
  extract_list(c, params)
  return params


def param_name(t):
  assert isinstance(t, lark.tree.Tree)
  c = t.children[1]
  assert isinstance(c, lark.tree.Tree)
  assert c.data == 'param_name'
  token = c.children[0]
  assert isinstance(token, lark.lexer.Token)
  return token.value


def param_type(t):
  assert isinstance(t, lark.tree.Tree)
  c = t.children[0]
  assert isinstance(c, lark.tree.Tree)
  return c


def get_return_value(rtype, rname, param, var, ref_param):
  crtype = type_core(rtype)
  if type_is_const(rtype) or type_is_refptr(rtype, '&'):
    # If the return type is a const or a reference, return the matching
    # parameter. In these cases we operated on XLA tensors data (the ATEN one),
    # but the returned references are the input parameters.
    assert param
    return param_name(param)
  elif crtype != 'Tensor':
    return rname
  else:
    # If instead the return type is a value Tensor, we create a new one by
    # wrapping the proper local variable which has been created by calling
    # into the CPU tensor implementation.
    return 'bridge::CreateXlaTensor({}, bridge::GetXlaDevice({}))'.format(
        rname, param_name(ref_param))


def get_reference_param(params, fnopts=None):
  # The reference parameter is the Tensor object which we use to extract the
  # result Tensor device, if any.
  ref_param = None
  other = None
  for p in params:
    ptype = param_type(p)
    cptype = type_core(ptype)
    pname = param_name(p)
    if fnopts and fnopts.ref_param == pname:
      return p
    if not other and (cptype == 'TensorOptions' or cptype == 'TensorList'):
      other = p
    if cptype != 'Tensor':
      continue
    if not ref_param and (pname == 'self' or type_is_const(ptype)):
      ref_param = p
    other = p
  return ref_param or other


def get_tuple_return(rtype, rtype_str, rname, params, param_vars, ref_param):
  types = tuple_type_list(rtype)
  retstr = '{}('.format(rtype_str)
  for i, ttype in enumerate(types):
    if i > 0:
      retstr += ', '
    tuple_var = 'std::get<{}>({})'.format(i, rname)
    retstr += get_return_value(ttype, tuple_var, list_get(params, i),
                               list_get(param_vars, i), ref_param)
  return retstr + ')'


def get_return_type_str(t, orig_sig):
  assert isinstance(t, lark.tree.Tree)
  fname = t.children[1]
  assert isinstance(fname, lark.tree.Tree)
  assert fname.data == 'fnname'
  token = fname.children[0]
  assert isinstance(token, lark.lexer.Token)
  return orig_sig[0:token.column - 2]


def generate_debug_code(t, fname, rname, params, param_vars, ref_param):
  # Emits debug code for a given intercepted ATEN type function. For now we use
  # a counter which will show up in the metrics reports.
  code = ''
  code += '  XLA_COUNTER("aten::{}", 1);\n'.format(fname)
  return code


def generate_return_stmt(t, rtype_str, fname, rname, params, param_vars,
                         ref_param):
  assert isinstance(t, lark.tree.Tree)
  rtype = t.children[0]
  ctype = type_core(rtype)
  if ctype == 'std::tuple':
    retstr = get_tuple_return(rtype, rtype_str, rname, params, param_vars,
                              ref_param)
  elif ctype == 'std::vector':
    retstr = 'bridge::CreateXlaTensors({}, bridge::GetXlaDevice({}))'.format(
        rname, param_name(ref_param))
  elif ctype == 'Tensor':
    retstr = get_return_value(rtype, rname, params[0], param_vars[0], ref_param)
  elif ctype == 'void' and not type_is_refptr(rtype, '*'):
    return ''
  else:
    retstr = rname
  return '  return {};\n'.format(retstr)


def generate_result_assignment(t, rname):
  assert isinstance(t, lark.tree.Tree)
  rtype = t.children[0]
  ctype = type_core(rtype)
  if ctype == 'void' and not type_is_refptr(rtype, '*'):
    return ''
  return 'auto&& {} = '.format(rname)


def get_handling_function(ctx, fname, xla_ref_param):
  xla_function = _XLA_FUNCTIONS.get(fname, None)
  return xla_function or ctx.get_function(fname, xla_ref_param)


def rewrite_tensor_options(fname, pname):
  rw = _CTOR_FUNCTIONS.get(fname, None)
  if rw is None:
    return '', pname
  xname = 'o_{}'.format(pname)
  code = '  at::TensorOptions {} = {}{};\n'.format(xname, pname, rw)
  return code, xname


def get_xla_wrapper(orig_sig, ctx):
  tree = _PARSER.parse(orig_sig)
  xtree = _XPARSER.parse(orig_sig)
  mapsig = create_map_sig(xtree, orig_sig)
  rwsig = rewrite_signature(orig_sig, _TYPE_NSMAP)
  rwxtree = _XPARSER.parse(rwsig)
  params = get_parameters(tree)
  fnopts = _FUNCTION_OPTIONS.get(mapsig, None)
  ref_param = get_reference_param(params, fnopts=fnopts)

  # There are a few functions with the same function name but different
  # parameter list. Generate a unique XL function name here.
  def gen_fnname(x):
    if ctx.gen_class_mode:
      return 'AtenXlaTypeBase::{}'.format(x)
    post = ''
    if x in ctx.defdb:
      post = '_{}'.format(ctx.defdb[x])
      ctx.defdb[x] += 1
    else:
      ctx.defdb[x] = 1
    return 'xla_' + x + post

  sig, fname, xfname = get_function_signature(rwxtree, rwsig, gen_fnname)
  if is_blacklisted_fn(fname, mapsig):
    return None

  code = '{} {}{{\n'.format(sig, 'const ' if ctx.gen_class_mode else '')
  xla_ref_param = param_name(ref_param) if ref_param else None
  tfetcher = TensorFetcher('xlatens')
  param_vars = []
  for p in params:
    ptype = param_type(p)
    cptype = type_core(ptype)
    pname = param_name(p)
    if cptype == 'TensorList':
      xname = 'l_{}'.format(pname)
      code += ('  auto {} = bridge::XlaCreateTensorList({}, '
               '/*writeable=*/nullptr);\n').format(xname, pname)
      param_vars.append(xname)
    elif cptype == 'TensorOptions':
      gcode, xname = rewrite_tensor_options(fname, pname)
      code += gcode
      param_vars.append(xname)
    elif cptype != 'Tensor':
      param_vars.append(pname)
    elif type_is_const(ptype):
      xname = tfetcher.add(pname, False)
      param_vars.append(xname)
    else:
      xname = tfetcher.add(pname, True)
      param_vars.append(xname)
    if p == ref_param and not (fnopts and fnopts.ref_param):
      xla_ref_param = param_vars[-1]
  code += tfetcher.generate()
  result_assign = generate_result_assignment(tree, _RESULT_NAME)
  code += '  {}{}('.format(result_assign,
                           get_handling_function(ctx, fname, xla_ref_param))
  for i, v in enumerate(param_vars):
    if i > 0:
      code += ', '
    code += v
  code += ');\n'
  if result_assign:
    code += ('  static_cast<void>({}); // Avoid warnings in case not '
             'used\n'.format(_RESULT_NAME))
  code += generate_debug_code(tree, fname,
                              _RESULT_NAME if result_assign else None, params,
                              param_vars, ref_param)
  code += generate_return_stmt(tree, get_return_type_str(rwxtree, rwsig), fname,
                               _RESULT_NAME if result_assign else None, params,
                               param_vars, ref_param)
  code += '}'
  return FuncGen(
      tree=tree,
      xtree=xtree,
      rwxtree=rwxtree,
      func=fname,
      xfunc=xfname,
      code=code,
      sig=orig_sig,
      rwsig=rwsig,
      cppsig=sig,
      funsig=create_stdfunc_sig(rwxtree, rwsig),
      mapsig=mapsig)


def extract_functions(path):
  functions = []
  for line in open(path, 'r'):
    m = re.match(r'\s*([^\s].*) const override;', line)
    if not m:
      continue
    fndef = m.group(1)
    try:
      _XPARSER.parse(fndef)
      functions.append(fndef)
    except:
      pass
  return functions


def generate_registrations(fgens):
  code = 'void RegisterAtenTypeFunctions() {\n'
  for fgen in fgens:
    code += ('  at::register_extension_backend_op(\n    at::Backend::XLA,\n    '
             '"{}",\n    &{});\n'.format(fgen.mapsig, fgen.xfunc))
  return code + '}\n'


def generate_functions(fgens):
  code = ''
  for fgen in fgens:
    code += '{}\n\n'.format(fgen.code)
  return code


def generate_class_functions(fgens):
  code = ''
  for fgen in fgens:
    code += '  {} const override;\n'.format(fgen.rwsig)
  return code


def gen_output_file(args, name):
  if not args.output_folder:
    return sys.stdout
  return open(os.path.join(args.output_folder, name), 'w')


def gen_cpp_output_file(args):
  return gen_output_file(args, 'aten_xla_type_base.cpp')


def gen_h_output_file(args):
  return gen_output_file(args, 'aten_xla_type_base.h')


def gen_h_instances_output_file(args):
  return gen_output_file(args, 'aten_xla_type_instances.h')


def generate(args):
  fndefs = extract_functions(args.typedef)
  print(
      'Extracted {} functions from {}'.format(len(fndefs), args.typedef),
      file=sys.stderr)
  fgens = []
  ctx = Context(args.functions, args.native_functions, args.gen_class_mode)
  for ts in fndefs:
    try:
      fgen = get_xla_wrapper(ts, ctx)
      if fgen:
        fgens.append(fgen)
    except Exception as e:
      print(
          'File to generate wrapper for {}: {}'.format(ts, e), file=sys.stderr)
  print(
      'Generated {} wrappers for {}'.format(len(fgens), args.typedef),
      file=sys.stderr)

  functions = generate_functions(fgens)
  if args.gen_class_mode:
    hfunctions = generate_class_functions(fgens)
    print(
        _H_CLASS_HEADER.format(
            gen=os.path.basename(sys.argv[0]), hfuncs=hfunctions),
        file=gen_h_output_file(args))
    print(
        _CPP_CLASS_HEADER.format(
            gen=os.path.basename(sys.argv[0]),
            funcs=functions,
            backend='at::Backend::XLA',
            device_type='at::DeviceType::XLA',
            typeid='at::TypeID::XLA'),
        file=gen_cpp_output_file(args))
    instances = create_type_instances()
    print(
        _CLASS_INSTANCES_HEADER.format(
            gen=os.path.basename(sys.argv[0]), instances=instances),
        file=gen_h_instances_output_file(args))
  else:
    regs = generate_registrations(fgens)
    print(
        _CPP_HEADER.format(
            gen=os.path.basename(sys.argv[0]), funcs=functions, regs=regs),
        file=gen_cpp_output_file(args))


if __name__ == '__main__':
  arg_parser = argparse.ArgumentParser()
  arg_parser.add_argument('--output_folder', type=str)
  arg_parser.add_argument('--gen_class_mode', action='store_true')
  arg_parser.add_argument(
      'typedef',
      type=str,
      metavar='TYPE_DEFAULT_FILE',
      help='The path to the TypeDefault.h file')
  arg_parser.add_argument(
      'functions',
      type=str,
      metavar='FUNCTIONS_FILE',
      help='The path to the Functions.h file')
  arg_parser.add_argument(
      'native_functions',
      type=str,
      metavar='NATIVE_FUNCTIONS_FILE',
      help='The path to the NativeFunctions.h file')
  args, files = arg_parser.parse_known_args()
  generate(args)
