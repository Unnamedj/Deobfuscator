"""
ZEUS lifter: walks the dispatcher's leaves into the shared lifter IR (ir.py)
and hands every function to the shared back end (backend.lower).

A *function* is a set of leaves reached from an entry state. Each leaf is run
once per distinct incoming facts (sparse constant propagation, like the other
front ends): the dispatcher's ~100 locals are the registers, constants fold,
and what is only known at compile time is tracked as an abstract value:

  Cell(site)   a captured variable (`h = alloc()`; `w[h]` reads/writes it):
               the variable becomes a register captured by the closures
  UpCell(i)    the i-th cell the closure received (`N[i]`): Upval(i)
  ArgsT        the argument array (`q`): parameters
  Maker        closure constructors: `maker(entry, {cells})` is a ClosureExpr

The next state is the dispatcher's state register at the end of the leaf: a
constant, `cond and A or B` (a branch), or a global read of a made-up name
(nil: the function returns the array register).
"""
import os
import sys

import backend
import codegen as CG
import ir
import luasym as S
from ir import (Assign, CallStmt, Next, Ret, Node, Close, Crash, fmt_expr)  # noqa: F401
from luasym import (Expr, Const, Reg, Global, Upval, Index, Bin, Un, TempVal, Vararg, ClosureExpr,
                    Multi, TempTail, VarargTail, Unsupported)
from obfuscators.zeus.model import Maker, Special

CELL_BASE = 1000        # captured variables of this function -> Reg(CELL_BASE + n)
PARAM_BASE = 2000       # parameters of a fixed-arity function -> Reg(PARAM_BASE + i)
SNAP_BASE = 3000        # copies of a register's incoming value (see Walk.run_leaf)
TMP_BASE = 4000         # copies made for a parallel assignment
SPILL_BASE = 5000       # the spill table's slots: SPILL_BASE + key
MAX_STATES = 60000
MAX_PASSES = 3


# --------------------------------------------------------------------------
# values

class V:
    """A run-time value: `e` computes it now (registers as they are), `t` in
    terms of the leaf's incoming registers (for deciding the next state).
    `pack`: the register this value was read from holds a table of known items."""
    __slots__ = ("e", "t", "pack")

    def __init__(self, e, t=None, pack=None):
        self.e, self.t, self.pack = e, e if t is None else t, pack


class PackV:
    """What a register holds after `r = {a, b, f()}` when the items cannot change
    (constants, call results): reads `r[k]` give the item and the table goes dead
    when nothing else reads it. A fact, so it survives into the next leaves."""
    __slots__ = ("items", "tail")

    def __init__(self, items, tail):
        self.items, self.tail = tuple(items), tail

    def key(self):
        return (tuple(repr(x) for x in self.items),
                None if self.tail is None else (type(self.tail).__name__, getattr(self.tail, "t", None),
                                                self.tail.start))

    def __eq__(self, o):
        return isinstance(o, PackV) and o.key() == self.key()

    def __hash__(self):
        return hash(self.key())


class Cell:
    __slots__ = ("site",)

    def __init__(self, site):
        self.site = site

    def __eq__(self, o):
        return isinstance(o, Cell) and o.site == self.site

    def __hash__(self):
        return hash(("cell", self.site))

    def __repr__(self):
        return "Cell%s" % (self.site,)


class UpCell:
    __slots__ = ("idx",)

    def __init__(self, idx):
        self.idx = idx

    def __eq__(self, o):
        return isinstance(o, UpCell) and o.idx == self.idx

    def __hash__(self):
        return hash(("upcell", self.idx))

    def __repr__(self):
        return "UpCell(%d)" % self.idx


class Marker:
    """A compile-time singleton (the argument array, the cell list, the constant table)."""
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


SPILL = Marker("SPILL")
ARGS = Marker("ARGS")
CELLLIST = Marker("CELLLIST")
CONSTS = Marker("CONSTS")


def is_plain(v):
    return v is None or isinstance(v, (bool, int, float, bytes))


def is_static(v):
    """Known at compile time (what facts may hold)."""
    return is_plain(v) or isinstance(v, (Cell, UpCell, Marker, Special, Maker, PackV))


def same(a, b):
    if is_plain(a) and is_plain(b):
        if isinstance(a, bool) or isinstance(b, bool):
            return a is b
        if isinstance(a, bytes) != isinstance(b, bytes):
            return False
        return a == b and (a is None) == (b is None)
    if isinstance(a, (Special, Maker)) and isinstance(b, (Special, Maker)):
        return a is b
    return a == b


def meet(a, b):
    """Facts true on both incoming paths."""
    out = {}
    for n, v in a.items():
        w = b.get(n, Ellipsis)
        if w is not Ellipsis and same(v, w):
            out[n] = v
    return out


def facts_equal(a, b):
    return a.keys() == b.keys() and all(same(v, b[n]) for n, v in a.items())


def truthy(v):
    return not (v is None or v is False)


class ZState:
    """Successor reference of a Next outcome."""
    __slots__ = ("leaf", "fin", "mode", "pc")

    def __init__(self, leaf, fin):
        self.leaf, self.fin = leaf, fin
        self.mode, self.pc = "Z", leaf

    def key(self, link=None):
        return ("Z", self.leaf, self.fin)


# --------------------------------------------------------------------------
# trees -> branches

class RegLeaf(Expr):
    """Tree leaf: register n after `ver` writes within the leaf; `inner` is the tree it was
    assigned (None: its incoming value, or a variable that closures may write)."""

    def __init__(self, n, ver, inner=None):
        self.n, self.ver, self.inner = n, ver, inner


def unwrap(t):
    while isinstance(t, RegLeaf) and t.inner is not None:
        t = t.inner
    return t


def decide(t):
    """Next-state tree -> ("leaf", const) | ("if", cond tree, then, else)."""
    t = unwrap(t)
    if isinstance(t, Const):
        return ("leaf", t.v)
    if isinstance(t, Global):
        return ("leaf", None)          # made-up global: nil, the function ends
    if isinstance(t, Bin) and t.op == "Or":
        l, r = unwrap(t.a), t.b
        if isinstance(l, Bin) and l.op == "And" and isinstance(unwrap(l.b), Const) and truthy(unwrap(l.b).v):
            return ("if", l.a, ("leaf", unwrap(l.b).v), decide(r))
        if isinstance(l, Const):
            return decide(l) if truthy(l.v) else decide(r)
    if isinstance(t, Bin) and t.op == "And" and isinstance(unwrap(t.b), Const) and truthy(unwrap(t.b).v):
        return ("if", t.a, ("leaf", unwrap(t.b).v), ("leaf", None))
    raise Unsupported("computed jump %s" % fmt_expr(t))


def memory_free(t):
    """Does the tree compute the same value later (no table, global or upvalue reads)?"""
    stack = [t]
    while stack:
        x = stack.pop()
        if isinstance(x, RegLeaf):
            if x.inner is not None:
                stack.append(x.inner)
        elif isinstance(x, (Index, Global, Upval)):
            return False
        else:
            stack.extend(CG.children(x))
    return True


# --------------------------------------------------------------------------

class LeafRun:
    """One evaluation of one leaf with given incoming facts."""

    def __init__(self, walk, leaf, fin, facts, snap):
        self.w = walk
        self.m = walk.m
        self.leaf = leaf
        self.fin = fin
        self.out = []
        self.val = {}
        self.written = set()
        self.snap = snap            # {reg: snapshot register}
        self.ncalls = 0
        self.nalloc = 0
        self.ctor = {}              # reg -> the constructor it still holds (make_return)
        self.tick = 0
        self.last_write = {}        # reg -> tick of its last write
        self.nwrites = {}           # reg -> number of writes so far
        self.val.update(facts)
        if leaf.reads_state:
            self.val[0] = fin       # the state register itself (reg 0)
        else:
            self.val.pop(0, None)
        self.state_reg = 0

    # ---- registers
    def read_reg(self, n):
        v = self.val.get(n, Ellipsis)
        if v is Ellipsis:
            # incoming value of a register nothing assigned yet in this leaf
            return V(Reg(n), RegLeaf(n, self.nwrites.get(n, 0)))
        if isinstance(v, V):
            return V(Reg(n), RegLeaf(n, self.nwrites.get(n, 0), v.t))
        if isinstance(v, PackV):
            return V(Reg(n), RegLeaf(n, self.nwrites.get(n, 0)), v)
        return v

    def unpack_reg(self, b):
        """b read from a register that holds a PackV, else None"""
        if isinstance(b, V) and isinstance(b.pack, PackV):
            return b.pack
        return None

    def drop_pack(self, v):
        """The register behind value v is used as a whole: forget what it held."""
        if isinstance(v, V) and isinstance(v.e, Reg) and isinstance(self.val.get(v.e.n), PackV):
            self.val[v.e.n] = V(Reg(v.e.n), RegLeaf(v.e.n, self.nwrites.get(v.e.n, 0)))

    def before_write(self, n):
        """A register is about to be overwritten: keep the copies the next-state decision needs."""
        key = (n, self.nwrites.get(n, 0))
        if key in self.snap:
            self.out.append(Assign(Reg(self.snap[key]), Reg(n)))
        self.nwrites[n] = key[1] + 1
        self.written.add(n)
        self.ctor.pop(n, None)
        self.tick += 1
        self.last_write[n] = self.tick

    def set_reg(self, n, v):
        self.before_write(n)
        if isinstance(v, V):
            self.out.append(Assign(Reg(n), v.e))
            self.val[n] = V(Reg(n), v.t)
        elif is_plain(v):
            self.out.append(Assign(Reg(n), Const(v)))
            self.val[n] = v
        else:
            if not is_static(v):
                raise Unsupported("cannot keep %r in a register" % (v,))
            self.val[n] = v          # compile-time value: nothing to emit

    def set_cell(self, cell, v):
        n = self.w.cell_reg(cell.site)
        self.before_write(n)
        self.out.append(Assign(Reg(n), self.E(v)))
        self.val.pop(n, None)          # (closures write cells too: never a known value)

    # ---- expressions
    def E(self, v):
        if isinstance(v, V):
            return v.e
        if is_plain(v):
            return Const(v)
        raise Unsupported("compile-time value %r used at run time" % (v,))

    def T(self, v):
        if isinstance(v, V):
            return v.t
        if is_plain(v):
            return Const(v)
        raise Unsupported("compile-time value %r used at run time" % (v,))

    def local(self, node):
        loc = node["local"]["location"]
        n = self.m.regs.get(loc)
        if n is not None:
            return self.read_reg(n)
        r = self.m.roles.get(loc)
        if r is None:
            raise Unsupported("unknown wrapper local %s" % node["local"]["name"])
        return r

    def expr(self, node):
        t = node["type"]
        if t == "AstExprConstantNumber":
            return S.fix_int(node["value"])
        if t == "AstExprConstantString":
            return node["value"].encode("latin-1")
        if t == "AstExprConstantBool":
            return node["value"]
        if t == "AstExprConstantNil":
            return None
        if t == "AstExprLocal":
            return self.local(node)
        if t == "AstExprGroup":
            return self.single(self.expr(node["expr"]))
        if t == "AstExprIndexExpr":
            return self.index(self.expr(node["expr"]), self.expr(node["index"]))
        if t == "AstExprIndexName":
            return self.index(self.expr(node["expr"]), node["index"].encode("latin-1"))
        if t == "AstExprCall":
            return self.first(self.call(node))
        if t == "AstExprBinary":
            return self.binary(node)
        if t == "AstExprUnary":
            return self.unary(node["op"], self.expr(node["expr"]))
        if t == "AstExprTable":
            return self.table(node)
        raise Unsupported("expression %s" % t)

    def single(self, v):
        return v.first() if isinstance(v, Multi) else v

    def first(self, mv):
        if mv.items:
            return mv.items[0]
        if mv.tail is not None:
            tv = mv.tail.at(1)
            return V(tv, tv)
        return None

    def index(self, b, k):
        if b is SPILL:
            if not isinstance(k, int) or isinstance(k, bool):
                raise Unsupported("spill table indexed by %r" % (k,))
            return self.read_reg(SPILL_BASE + k)
        if b is CONSTS:
            if not isinstance(k, (int, float)) or int(k) not in self.m.consts:
                raise Unsupported("constant table index %r" % (k,))
            return self.m.consts[int(k)]
        if b is CELLLIST:
            if not isinstance(k, (int, float)):
                raise Unsupported("cell list index %r" % (k,))
            return UpCell(int(k))
        if isinstance(b, Special) and b.kind == "cells":
            if isinstance(k, Cell):
                n = self.w.cell_reg(k.site)
                return V(Reg(n), RegLeaf(n, self.nwrites.get(n, 0)))
            if isinstance(k, UpCell):
                return V(Upval(k.idx), Upval(k.idx))
            raise Unsupported("cell store read through %r" % (k,))
        if b is ARGS:
            if not isinstance(k, (int, float)):
                raise Unsupported("argument index %r" % (k,))
            return self.arg(int(k))
        if isinstance(b, Special) and b.kind == "env":
            if isinstance(k, bytes):
                g = Global(k.decode("latin-1"))
                return V(g, g)
            raise Unsupported("dynamic global access")
        if isinstance(b, bytes):
            # a method of a string constant: ("fmt"):format(...)
            return V(Index(Const(b), self.E(k)), Index(Const(b), self.T(k)))
        if b is None or is_plain(b):
            raise Unsupported("index of %r" % (b,))
        if not isinstance(b, V):
            raise Unsupported("index of %r" % (b,))
        sm = self.w.strings
        if sm and isinstance(k, int) and not isinstance(k, bool) and k in sm and sm[k][0] == "key":
            return sm[k][1]             # the string table a decryptor key stands for
        hit = self.pack_item(b, k)
        if hit is not None:
            return hit
        if not (isinstance(k, V) or is_plain(k)):
            raise Unsupported("index by %r" % (k,))
        return V(Index(b.e, self.E(k)), Index(b.t, self.T(k)))

    def pack_item(self, b, k):
        """`t[k]` of a register holding a PackV: the item itself."""
        p = self.unpack_reg(b)
        if p is None or not isinstance(k, int) or isinstance(k, bool) or k < 1:
            return None
        if k <= len(p.items):
            e = p.items[k - 1]
            return V(e, e)
        if p.tail is not None:
            tv = p.tail.at(k - len(p.items))
            return V(tv, tv)
        return None

    def arg(self, i):
        ar = self.w.arity
        if ar is None:
            e = Vararg(i)
            return V(e, e)
        if 1 <= i <= ar:
            e = Reg(PARAM_BASE + i)
            return V(e, RegLeaf(e.n, 0))
        return None

    def binary(self, node):
        op = node["op"]
        if op in ("And", "Or"):
            a = self.expr(node["left"])
            if not isinstance(a, V):
                if op == "And":
                    return self.expr(node["right"]) if truthy(a) else a
                return a if truthy(a) else self.expr(node["right"])
            mark = len(self.out)
            b = self.expr(node["right"])
            if len(self.out) != mark:
                raise Unsupported("call inside a short-circuit expression")
            return V(Bin(op, a.e, self.E(b)), Bin(op, a.t, self.T(b)))
        return self.binop(op, self.expr(node["left"]), self.expr(node["right"]))

    def binop(self, op, a, b):
        if is_plain(a) and is_plain(b):
            r = self.fold(op, a, b)
            if r is not Ellipsis:
                return r
        return V(Bin(op, self.E(a), self.E(b)), Bin(op, self.T(a), self.T(b)))

    def fold(self, op, a, b):
        num = (int, float)
        isnum = lambda x: isinstance(x, num) and not isinstance(x, bool)  # noqa: E731
        if op in ("Add", "Sub", "Mul", "Div", "FloorDiv", "Mod", "Pow"):
            if isnum(a) and isnum(b):
                return S.arith(op, a, b)
        elif op == "Concat":
            def s(x):
                if isinstance(x, bytes):
                    return x
                if isnum(x):
                    return S.fmt_num(x).encode()
                return None
            if s(a) is not None and s(b) is not None:
                return s(a) + s(b)
        elif op == "CompareEq":
            return S.lua_eq(a, b)
        elif op == "CompareNe":
            return not S.lua_eq(a, b)
        elif op in ("CompareLt", "CompareLe", "CompareGt", "CompareGe"):
            if (isnum(a) and isnum(b)) or (isinstance(a, bytes) and isinstance(b, bytes)):
                return {"CompareLt": a < b, "CompareLe": a <= b, "CompareGt": a > b, "CompareGe": a >= b}[op]
        return Ellipsis         # (a type error at run time: the script means it, e.g. a line-number probe)

    def unary(self, op, a):
        if is_plain(a):
            if op == "Not":
                return not truthy(a)
            if op == "Minus" and isinstance(a, (int, float)) and not isinstance(a, bool):
                return S.fix_int(-a)
            if op == "Len" and isinstance(a, bytes):
                return len(a)
            raise Unsupported("unary %s on %r" % (op, a))
        return V(Un(op, self.E(a)), Un(op, self.T(a)))

    def table(self, node):
        items, trees = [], []
        tail = None
        arr = node["items"]
        for i, it in enumerate(arr):
            last = i == len(arr) - 1
            if it["kind"] == "item":
                if last and it["value"]["type"] == "AstExprCall":
                    mv = self.call(it["value"])
                    for x in mv.items:
                        items.append((None, self.E(x)))
                        trees.append((None, self.T(x)))
                    tail = mv.tail
                else:
                    x = self.expr(it["value"])
                    items.append((None, self.E(x)))
                    trees.append((None, self.T(x)))
            else:
                k = self.expr(it["key"]) if it["kind"] == "general" else it["key"]["value"].encode("latin-1")
                x = self.expr(it["value"])
                items.append((self.E(k), self.E(x)))
                trees.append((self.T(k), self.T(x)))
        if tail is not None and all(k is None for k, _ in items):
            # `{a, f()}`: a packed list; the back end drops it when only the call's results are read
            te = S.SymList([e for _, e in items], tail)
            tt = S.SymList([e for _, e in trees], tail)
        elif tail is not None:
            te = CG.NewTableE(items, tail)
            tt = CG.NewTableE(trees, tail)
        else:
            te, tt = S.NewTable(items), S.NewTable(trees)
        return _Ctor(te, tt, items, trees, tail)

    # ---- calls
    def args_of(self, nodes):
        """Argument values: (items, tail)."""
        items = []
        tail = None
        for i, a in enumerate(nodes):
            if i == len(nodes) - 1 and a["type"] == "AstExprCall":
                mv = self.call(a)
                items.extend(mv.items)
                tail = mv.tail
            else:
                items.append(self.expr(a))
        return items, tail

    def call(self, node):
        fn = self.expr(node["func"])
        if isinstance(fn, Maker):
            return Multi([self.make_closure(fn, node["args"])])
        if isinstance(fn, Special):
            k = fn.kind
            if k == "alloc":
                site = (self.leaf.idx, self.nalloc)
                self.nalloc += 1
                n = self.w.cell_reg(site)
                self.before_write(n)
                self.out.append(Assign(Reg(n), Const(None)))     # a fresh variable
                self.val.pop(n, None)
                return Multi([Cell(site)])
            if k == "release":
                if len(node["args"]) != 1:
                    raise Unsupported("release call")
                c = self.expr(node["args"][0])
                if isinstance(c, Cell):
                    n = self.w.cell_reg(c.site)
                    self.out.append(Close(n))
                    self.written.add(n)
                return Multi([])
            if k == "unpack":
                items, tail = self.args_of(node["args"])
                if items and items[0] is ARGS and tail is None and len(items) == 1:
                    return self.unpack_args()
                p = self.unpack_reg(items[0]) if len(items) == 1 and tail is None else None
                if p is not None:
                    return Multi([V(e, e) for e in p.items], p.tail)
            if k == "select":
                items, tail = self.args_of(node["args"])
                if items and isinstance(items[0], (int, float)) and not isinstance(items[0], bool):
                    r = self.select(int(items[0]), items[1:], tail)
                    if r is not None:
                        return r
                return self.emit_call(Global("select"), items, tail)
            if k in ("unpack",):
                return self.emit_call(Global("table.unpack"), items, tail)
            raise Unsupported("call of %s" % k)
        items, tail = self.args_of(node["args"])
        if not isinstance(fn, V):
            raise Unsupported("call of %r" % (fn,))
        sm = self.w.strings
        if sm and tail is None and len(items) == 2 and isinstance(items[1], int) \
                and not isinstance(items[1], bool) and items[1] in sm:
            # a string constant: decrypt(<assembled pieces>, key) (see strings.py)
            mode, val = sm[items[1]]
            if isinstance(items[0], V) and isinstance(unwrap(items[0].t), TempVal):
                self.w.drop_calls.add(unwrap(items[0].t).t)
            return Multi([val if mode == "str" else items[1]])
        return self.emit_call(fn.e, items, tail)

    def unpack_args(self):
        ar = self.w.arity
        if ar is None:
            return Multi([], VarargTail(1))
        return Multi([self.arg(i) for i in range(1, ar + 1)])

    def select(self, n, items, tail):
        if n < 1:
            return None
        k = n - 1
        if k <= len(items):
            return Multi(items[k:], tail)
        if tail is not None:
            return Multi([], tail.drop(k - len(items)))
        return Multi([])

    def emit_call(self, fn_e, items, tail):
        for x in items:
            if isinstance(x, V) and isinstance(x.e, Reg):
                self.ctor.pop(x.e.n, None)
                self.drop_pack(x)
        tid = "%d_%d" % (self.leaf.idx, self.ncalls)
        self.ncalls += 1
        self.out.append(CallStmt(tid, fn_e, Multi([self.E(x) for x in items], tail)))
        return Multi([], TempTail(tid, 1))

    def make_closure(self, mk, args):
        if len(args) != 2 or args[1]["type"] != "AstExprTable":
            raise Unsupported("closure constructor arguments")
        entry = self.expr(args[0])
        if not isinstance(entry, int):
            raise Unsupported("closure entry %r" % (entry,))
        ups = []
        for it in args[1]["items"]:
            c = self.expr(it["value"])
            if isinstance(c, Cell):
                ups.append(Reg(self.w.cell_reg(c.site)))
            elif isinstance(c, UpCell):
                ups.append(Upval(c.idx))
            else:
                raise Unsupported("closure capture %r" % (c,))
        e = ClosureExpr((entry, mk.arity), ups)
        return V(e, e)

    # ---- statements
    def statement(self, st):
        if st["type"] != "AstStatAssign":
            raise Unsupported("statement %s" % st["type"])
        targets = st["vars"]
        # right-hand sides, in order (the last may be a multi-value call)
        n = len(targets)
        values = st["values"]
        vals = []
        tail = None
        for i, v in enumerate(values):
            if i == len(values) - 1 and v["type"] == "AstExprCall" and n > i:
                mv = self.call(v)
                vals.extend(mv.items)
                tail = mv.tail
            else:
                vals.append(self.single(self.expr(v)))
        j = 1
        while len(vals) < n:
            if tail is not None:
                tv = tail.at(j)
                vals.append(V(tv, tv))
                j += 1
            else:
                vals.append(None)
        vals = vals[:n]
        # a plain value that is a ctor: remember it as the register's constructor
        # lvalue sub-expressions first (they read registers as they are now)
        lvs = []
        for tg in targets:
            lvs.append(self.lvalue(tg))
        if n > 1:
            vals = self.protect(lvs, vals)
        for lv, v in zip(lvs, vals):
            self.store(lv, v)

    def lvalue(self, tg):
        t = tg["type"]
        if t == "AstExprLocal":
            n = self.m.regs.get(tg["local"]["location"])
            if n is None:
                raise Unsupported("assignment to wrapper local")
            return ("reg", n)
        if t in ("AstExprIndexExpr", "AstExprIndexName"):
            b = self.expr(tg["expr"])
            k = self.expr(tg["index"]) if t == "AstExprIndexExpr" else tg["index"].encode("latin-1")
            return ("index", b, k)
        raise Unsupported("assignment target %s" % t)

    def protect(self, lvs, vals):
        """Parallel assignment: copy right-hand sides that read a register an earlier target overwrites."""
        out = []
        assigned = set()
        for i, (lv, v) in enumerate(zip(lvs, vals)):
            if isinstance(v, V) and not isinstance(v.e, (Const,)):
                regs = {x.n for x in CG.walk(v.e) if isinstance(x, Reg)}
                if regs & assigned:
                    tmp = TMP_BASE + len(self.out)
                    self.out.append(Assign(Reg(tmp), v.e))
                    v = V(Reg(tmp), v.t)
            out.append(v)
            if lv[0] == "reg":
                assigned.add(lv[1])
            elif lv[0] == "index" and isinstance(lv[1], V):
                pass
        return out

    def store(self, lv, v):
        if lv[0] == "reg":
            if isinstance(v, _Ctor):
                v.tick = self.tick          # (before the write: items reading this register are stale after it)
                self.set_reg(lv[1], V(v.e, v.t))
                self.ctor[lv[1]] = v
                if all(k is None and isinstance(e, (Const, TempVal, Vararg)) for k, e in v.items) and (
                        v.items or v.tail is not None):
                    self.val[lv[1]] = PackV([e for _, e in v.items], v.tail)
                return
            if isinstance(v, Multi):
                v = self.first(v)
            self.set_reg(lv[1], v)
            return
        _, b, k = lv
        if isinstance(v, _Ctor):
            v = V(v.e, v.t)
        if b is SPILL:
            if not isinstance(k, int) or isinstance(k, bool):
                raise Unsupported("spill table written by %r" % (k,))
            self.set_reg(SPILL_BASE + k, v)
            return
        if isinstance(b, Special) and b.kind == "cells":
            if isinstance(k, Cell):
                self.set_cell(k, v)
                return
            if isinstance(k, UpCell):
                self.out.append(Assign(Upval(k.idx), self.E(v)))
                return
            raise Unsupported("cell store write through %r" % (k,))
        if isinstance(b, Special) and b.kind == "env":
            if isinstance(k, bytes):
                self.out.append(Assign(Global(k.decode("latin-1")), self.E(v)))
                return
            raise Unsupported("dynamic global write")
        if isinstance(b, V):
            if isinstance(b.e, Reg):
                self.ctor.pop(b.e.n, None)
            self.drop_pack(b)
            self.out.append(Assign(Index(b.e, self.E(k)), self.E(v)))
            return
        raise Unsupported("store into %r" % (b,))

    # ---- the whole leaf
    def run(self):
        for st in self.leaf.stmts:
            self.statement(st)
        fv = self.val.get(self.state_reg, Ellipsis)
        if fv is Ellipsis:
            raise Unsupported("the state register was never assigned")
        if isinstance(fv, V):
            tree = fv.t
        elif is_plain(fv):
            tree = Const(fv)
        else:
            raise Unsupported("state is %r" % (fv,))
        dec = decide(tree)
        # registers the decision reads that are overwritten later in the leaf need a copy
        need = set()
        self.collect(dec, need)
        return dec, need - set(self.snap)

    def collect(self, dec, need):
        if dec[0] == "if":
            self.resolve(dec[1], need)
            self.collect(dec[2], need)
            self.collect(dec[3], need)
        elif dec[1] is None or dec[1] is False:
            rp = self.ret_plan()
            if rp is not None:
                for k, e in rp[0]:
                    if k == "t":
                        self.resolve(e, need)

    def resolve(self, t, need):
        """A tree as an expression valid at the end of the leaf. A register that still holds
        what the tree read is read as it is; an overwritten one is rebuilt from the tree it was
        assigned (when that reads no memory) or taken from a copy made before the overwrite."""
        def fn(x):
            if isinstance(x, RegLeaf):
                if self.nwrites.get(x.n, 0) == x.ver:
                    return Reg(x.n)
                if x.inner is not None and memory_free(x.inner):
                    return CG.map_expr(x.inner, fn)
                key = (x.n, x.ver)
                if key in self.snap:
                    return Reg(self.snap[key])
                need.add(key)
                return Reg(x.n)
            return None
        return CG.map_expr(t, fn)

    def tree_expr(self, t):
        return self.resolve(t, set())

    def build(self, dec):
        """Node for a decision."""
        if dec[0] == "leaf":
            return self.outcome(dec[1])
        cond = self.tree_expr(dec[1])
        return Node([], cond=cond, then=self.build(dec[2]), els=self.build(dec[3]))

    def outcome(self, state):
        if state is None or state is False:
            return Node([], outcome=self.make_return())
        if isinstance(state, (int, float)) and not isinstance(state, bool):
            return Node([], outcome=Next(ZState(self.m.leaf_for(int(state)).idx, self.fin_of(int(state)))))
        raise Unsupported("next state %r" % (state,))

    def fin_of(self, state):
        return state if self.m.leaf_for(state).reads_state else None

    def ret_plan(self):
        """How the function's return values are written: [("e", expr) | ("t", tree)], tail; or None
        (then `table.unpack(<array register>)`)."""
        c = self.ctor.get(self.m.ret_reg)
        if c is None:
            p = self.val.get(self.m.ret_reg)
            if isinstance(p, PackV):               # made in an earlier leaf (stable items)
                return [("e", e) for e in p.items], p.tail
            return None
        if any(k is not None for k, _ in c.items):
            return None
        plan = []
        for (_, e), (_, tr) in zip(c.items, c.trees):
            regs = {x.n for x in CG.walk(e) if isinstance(x, Reg)}
            if all(self.last_write.get(n, 0) <= c.tick for n in regs):
                plan.append(("e", e))               # (still holds what it did when the table was made)
            else:
                plan.append(("t", tr))
        return plan, c.tail

    def make_return(self):
        rp = self.ret_plan()
        if rp is not None:
            plan, tail = rp
            return Ret(Multi([e if k == "e" else self.tree_expr(e) for k, e in plan], tail))
        ret = self.m.ret_reg
        tid = "%d_ret" % self.leaf.idx
        self.out.append(CallStmt(tid, Global("table.unpack"), Multi([Reg(ret)])))
        return Ret(Multi([], TempTail(tid, 1)))


class _Ctor(V):
    """A table constructor value: remembered for the register it is stored in (see make_return)."""
    __slots__ = ("items", "trees", "tail", "tick")

    def __init__(self, e, t, items, trees, tail):
        V.__init__(self, e, t)
        self.items, self.trees, self.tail = items, trees, tail
        self.tick = 0


# --------------------------------------------------------------------------

class Walk:
    """Constant propagation over the leaves of one function."""

    def __init__(self, model, entry, arity, strings=None):
        self.m = model
        self.entry = entry
        self.arity = arity
        self.strings = strings          # {key: (mode, plaintext)} from strings.oracle
        self.drop_calls = set()         # calls whose result a decrypted string replaced
        self.cells = {}

    def cell_reg(self, site):
        n = self.cells.get(site)
        if n is None:
            n = self.cells[site] = CELL_BASE + len(self.cells)
        return n

    def initial_facts(self):
        facts = {n: None for n in range(len(self.m.reg_names))}
        facts[1] = ARGS
        facts[2] = CELLLIST
        for n in self.m.const_regs:
            facts[n] = CONSTS
        for n in self.m.table_regs:
            facts.pop(n)            # a fresh table (made in the prologue)
        if self.m.spill_reg is not None:
            facts[self.m.spill_reg] = SPILL
            for k in self.m.spill_keys:
                facts[SPILL_BASE + k] = None
        return facts

    def run_leaf(self, key, facts):
        leaf = self.m.leaves[key[1]]
        snap = {}
        for _ in range(MAX_PASSES):
            run = LeafRun(self, leaf, key[2], facts, snap)
            dec, need = run.run()
            if not need:
                break
            for k in sorted(need):
                snap.setdefault(k, SNAP_BASE + len(snap))
        else:
            raise Unsupported("snapshot passes did not settle")
        node = run.build(dec)
        node.stmts = run.out
        out = {n: v for n, v in run.val.items() if is_static(v) and n != 0}
        return node, out

    def run(self, limit=MAX_STATES):
        leaf0 = self.m.leaf_for(self.entry)
        k0 = ("Z", leaf0.idx, self.entry if leaf0.reads_state else None)
        facts_in = {k0: self.initial_facts()}
        nodes = {}
        order = []
        work = [k0]
        inwork = {k0}
        steps = 0
        while work:
            k = work.pop()
            inwork.discard(k)
            steps += 1
            if steps > limit * 4:
                raise Unsupported("state limit")
            try:
                node, fout = self.run_leaf(k, facts_in[k])
            except Unsupported as e:
                node = Node([], outcome=None)
                node.error = "%s (leaf %d)" % (e, k[1])
                fout = {}
            if k not in nodes:
                order.append(k)
            nodes[k] = node
            for nk in _next_keys(node):
                if nk not in facts_in:
                    facts_in[nk] = dict(fout)
                else:
                    mf = meet(facts_in[nk], fout)
                    if facts_equal(mf, facts_in[nk]):
                        continue
                    facts_in[nk] = mf
                if nk not in inwork:
                    work.append(nk)
                    inwork.add(nk)
        out = [(k, nodes[k]) for k in order]
        if self.drop_calls:
            mark_droppable([n for _, n in out], self.drop_calls)
        return k0, out


def mark_droppable(nodes, droppable):
    """The calls (string assembly) whose result a decrypted string replaced: the back end drops
    them once nothing reads their temp."""
    def mark(n):
        if n is None:
            return
        for s in n.stmts:
            if isinstance(s, CallStmt) and s.t in droppable:
                s.droppable = True
        mark(n.then)
        mark(n.els)
    for n in nodes:
        mark(n)


def _next_keys(node, out=None):
    out = [] if out is None else out
    if node is None:
        return out
    if isinstance(node.outcome, Next):
        out.append(node.outcome.state.key())
    _next_keys(node.then, out)
    _next_keys(node.els, out)
    return out


# --------------------------------------------------------------------------

INLINE_CONST_LOCALS = True
from ir import *  # noqa: E402,F401,F403  (the back end looks the IR classes up on this module)


class Program:
    def __init__(self, model, strings=None):
        self.m = model
        self.strings = strings
        self.stats = {"functions": 0, "errors": 0, "fallbacks": 0}
        self.lifting = set()
        self.memo = {}

    def lift(self, proto, upnames, depth=0):
        entry, arity = proto
        self.stats["functions"] += 1
        self.lifting.add(proto)
        try:
            wk = Walk(self.m, entry, arity, self.strings)
            k0, order = wk.run()
            if os.environ.get("ZEUS_RAW"):
                with open(os.path.join(os.environ["ZEUS_RAW"], "fn_%d.txt" % entry), "w") as f:
                    for k, node in order:
                        f.write("== %s\n" % (k,))
                        for ln in ir.fmt_node(node, "   "):
                            f.write(ln + "\n")
            pro = [Assign(Reg(n), S.NewTable([])) for n in self.m.table_regs]
            if arity:
                # fixed arity: parameters are the leading `local x = <vararg i>` statements
                pro = [Assign(Reg(PARAM_BASE + i), Vararg(i)) for i in range(1, arity + 1)] + pro
            if pro:
                ek = ("E", 0)
                order.insert(0, (ek, Node(pro, outcome=Next(_Ref(k0)))))
                k0 = ek
            me = sys.modules[__name__]
            prefix = "r" if depth == 0 else "r%d_" % depth
            lines, params, nerr, fallbacks = backend.lower(
                k0, order, me, None, prefix, upnames,
                lambda x, names: self.closure(x, names, depth))
            self.stats["errors"] += nerr
            self.stats["fallbacks"] += fallbacks
            return lines, params
        finally:
            self.lifting.discard(proto)

    def closure(self, c, parent_names, depth):
        proto = c.proto
        if proto in self.lifting:
            return CG.FuncE(["function(...) --[[ recursive function ]] end"])
        capnames = getattr(c, "capnames", {})
        upnames = {}
        for i, e in enumerate(c.upvals, 1):
            if isinstance(e, Reg):
                upnames[i] = capnames.get(e.n, "nil")
            elif isinstance(e, Upval):
                upnames[i] = parent_names.get(("up", e.idx), "upv%d" % e.idx)
            else:
                upnames[i] = "nil"
        key = (proto, depth, tuple(sorted(upnames.items())))
        if key in self.memo:
            lines, params = self.memo[key]
        else:
            try:
                lines, params = self.lift(proto, upnames, depth + 1)
            except Unsupported as ex:
                lines, params = ["error(\"devirt: could not lift closure: %s\")" % str(ex).replace("\"", "'")], ["..."]
            self.memo[key] = (lines, params)
        f = CG.FuncE(["function(%s)" % ", ".join(params)] + ["\t" + ln for ln in lines] + ["end"])
        f.captures = set(upnames.values())
        return f


class _Ref:
    """A Next target that is already a key."""
    def __init__(self, key):
        self._k = key
        self.mode, self.pc = key[0], key[1]

    def key(self, link=None):
        return self._k


def lift_program(model, use_strings=True):
    """(Luau text, Program). With string decryption the program is lifted twice: the first text
    is only used to find and run the script's string decryptor (strings.py)."""
    prog = Program(model)
    lines, params = prog.lift((model.entry, None), {})
    text = "\n".join(lines)
    if use_strings:
        from obfuscators.zeus import strings
        try:
            found = strings.oracle(text)
        except Exception as e:  # noqa: BLE001 - keep the first lift
            print("[!] string decryption failed (%s: %s)" % (type(e).__name__, e), file=sys.stderr)
            found = {}
        if found:
            print("[*] %d strings decrypted" % len(found), file=sys.stderr)
            prog = Program(model, found)
            lines, params = prog.lift((model.entry, None), {})
            text = "\n".join(lines)
    return text, prog
