import ctypes,ctypes.util,threading
class Collator:
    def __init__(self,locale='root',strength=2):
        if strength not in {0,1,2} or not isinstance(locale,str) or len(locale)>128 or '\0' in locale:raise ValueError("invalid collation profile")
        name=ctypes.util.find_library('icui18n') or ctypes.util.find_library('icucore')
        if not name:raise RuntimeError("ICU collation runtime is required")
        self.lib=ctypes.CDLL(name);self._lock=threading.RLock();self.handle=None
        def symbol(name):
            for suffix in ['']+['_'+str(i) for i in range(90,49,-1)]:
                try:return getattr(self.lib,name+suffix)
                except AttributeError:pass
            raise RuntimeError("ICU symbol unavailable: "+name)
        self.open=symbol('ucol_open');self.open.argtypes=[ctypes.c_char_p,ctypes.POINTER(ctypes.c_int32)];self.open.restype=ctypes.c_void_p
        self.close_fn=symbol('ucol_close');self.close_fn.argtypes=[ctypes.c_void_p];self.close_fn.restype=None
        self.sort=symbol('ucol_getSortKey');self.sort.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint16),ctypes.c_int32,ctypes.POINTER(ctypes.c_uint8),ctypes.c_int32];self.sort.restype=ctypes.c_int32
        set_strength=symbol('ucol_setStrength');set_strength.argtypes=[ctypes.c_void_p,ctypes.c_int32];set_strength.restype=None
        version=symbol('ucol_getVersion');version.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint8)];version.restype=None
        status=ctypes.c_int32(0);self.handle=self.open(locale.encode('ascii'),ctypes.byref(status))
        if status.value>0 or not self.handle:raise ValueError("ICU rejected locale")
        set_strength(self.handle,strength);v=(ctypes.c_uint8*4)();version(self.handle,v)
        self.profile={'locale':locale,'strength':strength,'icu_collation_version':'.'.join(map(str,v)),'warning':status.value}
    def key(self,text):
        if not isinstance(text,str) or len(text)>100000:raise ValueError("string limit")
        raw=text.encode('utf-16-le');a=(ctypes.c_uint16*(len(raw)//2)).from_buffer_copy(raw)
        with self._lock:
            if not self.handle:raise ValueError("collator closed")
            n=self.sort(self.handle,a,len(a),None,0)
            if not 0<n<=2_000_000:raise ValueError("sort key limit")
            dest=(ctypes.c_uint8*n)();actual=self.sort(self.handle,a,len(a),dest,n)
            if actual!=n:raise RuntimeError("unstable sort key size")
            return {'profile':dict(self.profile),'key':bytes(dest).hex()}
    def compare(self,a,b):
        ka,kb=bytes.fromhex(self.key(a)['key']),bytes.fromhex(self.key(b)['key'])
        return (ka>kb)-(ka<kb)
    @staticmethod
    def compare_keys(a,b):
        if a['profile']!=b['profile']:raise ValueError("incompatible sort-key profiles")
        x,y=bytes.fromhex(a['key']),bytes.fromhex(b['key']);return (x>y)-(x<y)
    def close(self):
        with self._lock:
            if self.handle:self.close_fn(self.handle);self.handle=None
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def __del__(self):
        if getattr(self,'handle',None):self.close()
