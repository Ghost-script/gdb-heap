'''
This file is licensed under the PSF license
'''
from past.builtins import long
from past.builtins import xrange

import gdb
from heap import WrappedPointer, caching_lookup_type, Usage, \
    type_void_ptr, fmt_addr, Category

type_size_t = gdb.lookup_type('size_t')
SIZEOF_VOID_P = type_void_ptr.sizeof

# Transliteration from Python's obmalloc.c:
ALIGNMENT             = 8
ALIGNMENT_SHIFT       = 3
ALIGNMENT_MASK        = (ALIGNMENT - 1)

# Return the number of bytes in size class I:
def INDEX2SIZE(I):
    return (I + 1) << ALIGNMENT_SHIFT

SYSTEM_PAGE_SIZE      = (4 * 1024)
SYSTEM_PAGE_SIZE_MASK = (SYSTEM_PAGE_SIZE - 1)
ARENA_SIZE            = (256 << 10)
POOL_SIZE             = SYSTEM_PAGE_SIZE
POOL_SIZE_MASK        = SYSTEM_PAGE_SIZE_MASK
def ROUNDUP(x):
    return (x + ALIGNMENT_MASK) & ~ALIGNMENT_MASK

def POOL_OVERHEAD():
    return ROUNDUP(caching_lookup_type('struct pool_header').sizeof)

class PyArenaPtr(WrappedPointer):
    # Wrapper around a (void*) that's a Python arena's buffer (the
    # arena->address, as opposed to the (struct arena_object*) itself)
    @classmethod
    def from_addr(cls, p, arenaobj):
        ptr = gdb.Value(p)
        ptr = ptr.cast(type_void_ptr)
        return cls(ptr, arenaobj)

    def __init__(self, gdbval, arenaobj):
        WrappedPointer.__init__(self, gdbval)

        assert(isinstance(arenaobj, ArenaObject))
        self.arenaobj = arenaobj

        # obmalloc.c sets up arenaobj->pool_address to the first pool
        # address, aligning it to POOL_SIZE_MASK:
        self.initial_pool_addr = self.as_address()
        self.num_pools = ARENA_SIZE / POOL_SIZE
        self.excess = self.initial_pool_addr & POOL_SIZE_MASK
        if self.excess != 0:
            self.num_pools -= 1
            self.initial_pool_addr += POOL_SIZE - self.excess

    def __str__(self):
        return ('PyArenaPtr([%s->%s], %i pools [%s->%s], excess: %i tracked by %s)'
                % (fmt_addr(self.as_address()),
                   fmt_addr(self.as_address() + ARENA_SIZE - 1),
                   self.num_pools,
                   fmt_addr(self.initial_pool_addr),
                   fmt_addr(self.initial_pool_addr
                            + (self.num_pools * POOL_SIZE) - 1),
                   self.excess,
                   self.arenaobj
                   )
                )

    def iter_pools(self):
        '''Yield a sequence of PyPoolPtr, representing all of the pools within
        this arena'''
        # print 'num_pools:', num_pools
        pool_addr = self.initial_pool_addr
        for idx in xrange(self.num_pools):

            # "pool_address" is a high-water-mark for activity within the arena;
            # pools at this location or beyond haven't been initialized yet:
            if pool_addr >= self.arenaobj.pool_address:
                return

            pool = PyPoolPtr.from_addr(pool_addr)
            yield pool
            pool_addr += POOL_SIZE

    def iter_usage(self):
        '''Yield a series of Usage instances'''
        if self.excess != 0:
            # FIXME: this size is wrong
            yield Usage(self.as_address(), self.excess, Category('pyarena', 'alignment wastage'))

        for pool in self.iter_pools():
            # print 'pool:', pool
            for u in pool.iter_usage():
                yield u

        # FIXME: unused space (if any) between pool_address and the alignment top

        # if self.excess != 0:
        #    # FIXME: this address is wrong
        #    yield Usage(self.as_address(), self.excess, Category('pyarena', 'alignment wastage'))


class PyPoolPtr(WrappedPointer):
    # Wrapper around Python's obmalloc.c: poolp: (struct pool_header *)

    @classmethod
    def from_addr(cls, p):
        ptr = gdb.Value(p)
        ptr = ptr.cast(cls.gdb_type())
        return cls(ptr)

    def __str__(self):
        return ('PyPoolPtr([%s->%s: %d blocks of size %i bytes))'
                % (fmt_addr(self.as_address()), fmt_addr(self.as_address() + POOL_SIZE - 1),
                   self.num_blocks(), self.block_size()))

    @classmethod
    def gdb_type(cls):
        # Deferred lookup of the "poolp" type:
        return caching_lookup_type('poolp')

    def block_size(self):
        return INDEX2SIZE(self.field('szidx'))

    def num_blocks(self):
        firstoffset = self._firstoffset()
        maxnextoffset = self._maxnextoffset()
        offsetrange = maxnextoffset - firstoffset
        return offsetrange / self.block_size() # FIXME: not exactly correctly

    def _firstoffset(self):
        return POOL_OVERHEAD()

    def _maxnextoffset(self):
        return POOL_SIZE - self.block_size()

    def iter_blocks(self):
        '''Yield all blocks within this pool, whether free or in use'''
        size = self.block_size()
        maxnextoffset = self._maxnextoffset()
        # print initnextoffset, maxnextoffset
        offset = self._firstoffset()
        base_addr = self.as_address()
        while offset <= maxnextoffset:
            yield (base_addr + offset, size)
            offset += size

    def iter_usage(self):
        # The struct pool_header at the front:
        yield Usage(self.as_address(),
                    POOL_OVERHEAD(),
                    Category('pyarena', 'pool_header overhead'))

        fb = list(self.iter_free_blocks())
        for (start, size) in fb:
            yield Usage(start, size, Category('pyarena', 'freed pool chunk'))

        for (start, size) in self.iter_used_blocks():
            if (start, size) not in fb:
                yield Usage(start, size) #, 'python pool: ' + categorize(start, size, None))

        # FIXME: yield any wastage at the end

    def iter_free_blocks(self):
        '''Yield the sequence of free blocks within this pool.  Doesn't include
        the areas after nextoffset that have never been allocated'''
        # print self._gdbval.dereference()
        size = self.block_size()
        freeblock = self.field('freeblock')
        _type_block_ptr_ptr = caching_lookup_type('unsigned char').pointer().pointer()
        # Walk the singly-linked list of free blocks for this chunk
        while long(freeblock) != 0:
            # print 'freeblock:', (fmt_addr(long(freeblock)), long(size))
            yield (long(freeblock), long(size))
            freeblock = freeblock.cast(_type_block_ptr_ptr).dereference()

    def _free_blocks(self):
        # Get the set of addresses of free blocks
        return set([addr for addr, size in self.iter_free_blocks()])

    def iter_used_blocks(self):
        '''Yield the sequence of currently in-use blocks within this pool'''
        # We'll filter out the free blocks from the list:
        free_block_addresses = self._free_blocks()

        size = self.block_size()
        initnextoffset = self._firstoffset()
        nextoffset = self.field('nextoffset')
        #print initnextoffset, nextoffset
        offset = initnextoffset
        base_addr = self.as_address()
        # Iterate upwards until you reach "pool->nextoffset": blocks beyond
        # that point have never been allocated:
        while offset < nextoffset:
            addr = base_addr + offset
            # Filter out those within this pool's linked list of free blocks:
            if long(addr) not in free_block_addresses:
                yield (long(addr), long(size))
            offset += size


Py_TPFLAGS_HEAPTYPE = (long(1) << 9)

Py_TPFLAGS_INT_SUBCLASS      = (long(1) << 23)
Py_TPFLAGS_LONG_SUBCLASS     = (long(1) << 24)
Py_TPFLAGS_LIST_SUBCLASS     = (long(1) << 25)
Py_TPFLAGS_TUPLE_SUBCLASS    = (long(1) << 26)
Py_TPFLAGS_STRING_SUBCLASS   = (long(1) << 27)
Py_TPFLAGS_UNICODE_SUBCLASS  = (long(1) << 28)
Py_TPFLAGS_DICT_SUBCLASS     = (long(1) << 29)
Py_TPFLAGS_BASE_EXC_SUBCLASS = (long(1) << 30)
Py_TPFLAGS_TYPE_SUBCLASS     = (long(1) << 31)

class PyObjectPtr(WrappedPointer):
    @classmethod
    def from_pyobject_ptr(cls, addr):
        ob_type = addr['ob_type']
        tp_flags = ob_type['tp_flags']
        if tp_flags & Py_TPFLAGS_HEAPTYPE:
            return HeapTypeObjectPtr(addr)

        if tp_flags & Py_TPFLAGS_DICT_SUBCLASS:
            return PyDictObjectPtr(addr.cast(caching_lookup_type('PyDictObject').pointer()))

        tp_name = ob_type['tp_name'].string()
        if tp_name == 'instance':
            __type_PyInstanceObjectPtr = caching_lookup_type('PyInstanceObject').pointer()
            return PyInstanceObjectPtr(addr.cast(__type_PyInstanceObjectPtr))

        return PyObjectPtr(addr)

    def type(self):
        return PyTypeObjectPtr(self.field('ob_type'))

    def safe_tp_name(self):
        try:
            return self.type().field('tp_name').string()
        except (RuntimeError, UnicodeDecodeError):
            # Can't even read the object at all?
            return 'unknown'

    def categorize(self):
        # Python objects will be categorized as ("python", tp_name), but
        # old-style classes have to do more work
        return Category('python', self.safe_tp_name())

    def categorize_refs(self, usage_set, level=0, detail=None):
        # do nothing by default:
        pass

    def as_malloc_addr(self):
        ob_type = addr['ob_type']
        tp_flags = ob_type['tp_flags']
        addr = long(self._gdbval)
        if tp_flags & Py_TPFLAGS_: # FIXME
            return obj_addr_to_gc_addr(addr)
        else:
            return addr

# Taken from my libpython.py code in python's Tools/gdb/libpython.py
# FIXME: ideally should share code somehow
def _PyObject_VAR_SIZE(typeobj, nitems):
    return ( ( typeobj.field('tp_basicsize') +
               nitems * typeobj.field('tp_itemsize') +
               (SIZEOF_VOID_P - 1)
             ) & ~(SIZEOF_VOID_P - 1)
           ).cast(type_size_t)
def int_from_int(gdbval):
    return int(gdbval)

class PyDictObjectPtr(PyObjectPtr):
    """
    Class wrapping a gdb.Value that's a PyDictObject* i.e. a dict instance
    within the process being debugged.
    """
    _typename = 'PyDictObject'

    def categorize_refs(self, usage_set, level=0, detail=None):
        ma_table = long(self.field('ma_table'))
        usage_set.set_addr_category(ma_table,
                                    Category('cpython', 'PyDictEntry table', detail),
                                    level)
        return True

class PyInstanceObjectPtr(PyObjectPtr):
    _typename = 'PyInstanceObject'

    def cl_name(self):
        in_class = self.field('in_class')
        # cl_name is a python string, not a char*; rely on
        # prettyprinters for now:
        cl_name = str(in_class['cl_name'])[1:-1]
        return cl_name

    def categorize(self):
        return Category('python', self.cl_name(), 'old-style')

    def categorize_refs(self, usage_set, level=0, detail=None):
        cl_name = self.cl_name()
        # print 'cl_name', cl_name

        # Visit the in_dict:
        in_dict = self.field('in_dict')
        # print 'in_dict', in_dict

        dict_detail = '%s.__dict__' % cl_name

        # Mark the ptr as being a dictionary, adding detail
        usage_set.set_addr_category(obj_addr_to_gc_addr(in_dict),
                                    Category('cpython', 'PyDictObject', dict_detail),
                                    level=1)

        # Visit ma_table:
        _type_PyDictObject_ptr = caching_lookup_type('PyDictObject').pointer()
        in_dict = in_dict.cast(_type_PyDictObject_ptr)

        ma_table = long(in_dict['ma_table'])

        # Record details:
        usage_set.set_addr_category(ma_table,
                                    Category('cpython', 'PyDictEntry table', dict_detail),
                                    level=2)
        return True

class PyTypeObjectPtr(PyObjectPtr):
    _typename = 'PyTypeObject'

class HeapTypeObjectPtr(PyObjectPtr):
    _typename = 'PyObject'

    def categorize_refs(self, usage_set, level=0, detail=None):
        attr_dict = self.get_attr_dict()
        if attr_dict:
            # Mark the dictionary's "detail" with our typename
            # gdb.execute('print (PyObject*)0x%x' % long(attr_dict._gdbval))
            usage_set.set_addr_category(obj_addr_to_gc_addr(attr_dict._gdbval),
                                        Category('python', 'dict', '%s.__dict__' % self.safe_tp_name()),
                                        level=level+1)

            # and mark the dict's PyDictEntry with our typename:
            attr_dict.categorize_refs(usage_set, level=level+1,
                                      detail='%s.__dict__' % self.safe_tp_name())
        return True

    def get_attr_dict(self):
        '''
        Get the PyDictObject ptr representing the attribute dictionary
        (or None if there's a problem)
        '''
        from heap import type_char_ptr
        try:
            typeobj = self.type()
            dictoffset = int_from_int(typeobj.field('tp_dictoffset'))
            if dictoffset != 0:
                if dictoffset < 0:
                    type_PyVarObject_ptr = caching_lookup_type('PyVarObject').pointer()
                    tsize = int_from_int(self._gdbval.cast(type_PyVarObject_ptr)['ob_size'])
                    if tsize < 0:
                        tsize = -tsize
                    size = _PyObject_VAR_SIZE(typeobj, tsize)
                    dictoffset += size
                    assert dictoffset > 0
                    if dictoffset % SIZEOF_VOID_P != 0:
                        # Corrupt somehow?
                        return None

                dictptr = self._gdbval.cast(type_char_ptr) + dictoffset
                PyObjectPtrPtr = caching_lookup_type('PyObject').pointer().pointer()
                dictptr = dictptr.cast(PyObjectPtrPtr)
                return PyObjectPtr.from_pyobject_ptr(dictptr.dereference())
        except RuntimeError:
            # Corrupt data somewhere; fail safe
            pass

        # Not found, or some kind of error:
        return None

def is_pyobject_ptr(addr):
    try:
        _type_pyop = caching_lookup_type('PyObject').pointer()
    except RuntimeError:
        # not linked against python
        return None

    pyop = gdb.Value(addr).cast(_type_pyop)
    try:
        ob_refcnt = pyop['ob_refcnt']
        if ob_refcnt >=0 and ob_refcnt < 0xffff:
            obtype = pyop['ob_type']
            if obtype != 0:
                type_refcnt = obtype['ob_refcnt']
                if type_refcnt > 0 and type_refcnt < 0xffff:
                    # Then this looks like a Python object:
                    return PyObjectPtr.from_pyobject_ptr(pyop)
    except (RuntimeError, UnicodeDecodeError):
        pass # Not a python object (or corrupt)

    # Doesn't look like a python object, implicit return None

def obj_addr_to_gc_addr(addr):
    '''Given a PyObject* address, convert to a PyGC_Head* address
    (i.e. the allocator's view of the same)'''
    #print 'obj_addr_to_gc_addr(%s)' % fmt_addr(long(addr))
    _type_PyGC_Head = caching_lookup_type('PyGC_Head')
    return long(addr) - _type_PyGC_Head.sizeof

def as_python_object(addr):
    '''Given an address of an allocation, determine if it holds a PyObject,
    or a PyGC_Head

    Return a WrappedPointer for the PyObject* if it does (which might have a
    different location c.f. when PyGC_Head was allocated)

    Return None if it doesn't look like a PyObject*'''
    # Try casting to PyObject* ?
    # FIXME: what about the debug allocator?
    try:
        _type_pyop = caching_lookup_type('PyObject').pointer()
    except RuntimeError:
        # not linked against python
        return None
    pyop = is_pyobject_ptr(addr)
    if pyop:
        return pyop
    else:
        # maybe a GC type:
        _type_PyGC_Head = caching_lookup_type('PyGC_Head')
        _type_PyGC_Head_ptr = _type_PyGC_Head.pointer()
        gc_ptr = gdb.Value(addr).cast(_type_PyGC_Head_ptr)
        # print gc_ptr.dereference()
        if gc_ptr['gc']['gc_refs'] == -3: #FIXME: need to cover other values
            pyop = is_pyobject_ptr(gdb.Value(addr + _type_PyGC_Head.sizeof))
            if pyop:
                return pyop
    # Doesn't look like a python object, implicit return None

class ArenaObject(WrappedPointer):
    '''
    Wrapper around Python's struct arena_object*
    Note that this is record-keeping for an arena, not the
    memory itself
    '''
    @classmethod
    def iter_arenas(cls):
        try:
            val_arenas = gdb.parse_and_eval('arenas')
            val_maxarenas = gdb.parse_and_eval('maxarenas')
        except RuntimeError:
            # Not linked against python, or no debug information:
            return

        for i in xrange(val_maxarenas):
            # Look up "&arenas[i]":
            obj = ArenaObject(val_arenas[i].address)

            # obj->address == 0 indicates an unused entry within the "arenas" array:
            if obj.address != 0:
                yield obj

    def __init__(self, gdbval):
        WrappedPointer.__init__(self, gdbval)

        # Cache some values:
        self.address = self.field('address')

        # This is the high-water mark: at this point and beyond, the bytes of
        # memory are untouched since malloc:
        self.pool_address = self.field('pool_address')

class ArenaDetection(object):
    '''Detection of Python arenas, done as an object so that we can cache state'''
    def __init__(self):
        self.arenaobjs = list(ArenaObject.iter_arenas())

    def as_py_arena(self, ptr, chunksize):
        '''Detect if this ptr returned by malloc is in use as a Python arena,
        returning PyArenaPtr if it is, None if not'''
        # Fast rejection of too-small chunks:
        if chunksize < (256 * 1024):
            return None

        for arenaobj in self.arenaobjs:
            if ptr == arenaobj.address:
                # Found it:
                return PyArenaPtr.from_addr(ptr, arenaobj)

        # Not found:
        return None


def python_categorization(usage_set):
    # special-cased categorization for CPython

    # The Objects/stringobject.c:interned dictionary is typically large,
    # with its PyDictEntry table occuping 200k on a 64-bit build of python 2.6
    # Identify it:
    try:
        val_interned = gdb.parse_and_eval('interned')
        pyop = PyDictObjectPtr.from_pyobject_ptr(val_interned)
        ma_table = long(pyop.field('ma_table'))
        usage_set.set_addr_category(ma_table,
                                    Category('cpython', 'PyDictEntry table', 'interned'),
                                    level=1)
    except RuntimeError:
        pass
