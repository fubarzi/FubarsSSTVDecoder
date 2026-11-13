"""Keep VB-CABLE's Windows Listen route out of the speaker monitor path."""
from __future__ import annotations

import ctypes as C
import uuid
import winreg


_CAPTURE = r'SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture'
_NAME = '{a45c254e-df1c-4efd-8020-67d146a850e0},2'
_DEVICE = '{b3f8fa53-0004-438e-9003-51a46e139bfc},6'
_LISTEN = '{24dbb0fc-9311-4b3d-9cf0-18ff155639d4},1'


class GUID(C.Structure):
    _fields_ = [('a', C.c_uint32), ('b', C.c_uint16), ('c', C.c_uint16),
                ('d', C.c_ubyte * 8)]


class PROPERTYKEY(C.Structure):
    _fields_ = [('fmtid', GUID), ('pid', C.c_uint32)]


class PROPVARIANT(C.Structure):
    _fields_ = [('vt', C.c_uint16), ('reserved1', C.c_uint16),
                ('reserved2', C.c_uint16), ('reserved3', C.c_uint16),
                ('value', C.c_ubyte * 16)]


def _guid(value):
    return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


def _check(hr, action):
    if hr < 0:
        raise OSError(f'{action} failed: 0x{hr & 0xffffffff:08X}')


def _call(pointer, index, restype, *types):
    address = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents[index]
    return C.WINFUNCTYPE(restype, C.c_void_p, *types)(address)


def _cable_endpoint():
    matches = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CAPTURE) as parent:
        index = 0
        while True:
            try:
                endpoint = winreg.EnumKey(parent, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(parent, endpoint + r'\Properties') as props:
                    name = winreg.QueryValueEx(props, _NAME)[0]
                    device = winreg.QueryValueEx(props, _DEVICE)[0]
                    if name == 'CABLE Output' and 'VB-Audio Virtual Cable' in device:
                        matches.append(endpoint)
            except OSError:
                continue
    if len(matches) > 1:
        raise RuntimeError('Multiple VB-CABLE recording devices found')
    return matches[0] if matches else None


def cable_listen_enabled():
    """Return Windows Listen state for standard VB-CABLE Output, or None."""
    found = _cable_endpoint()
    if found is None:
        return None
    endpoint = found
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CAPTURE + '\\' + endpoint + r'\Properties') as props:
            value, kind = winreg.QueryValueEx(props, _LISTEN)
        if kind == winreg.REG_BINARY and len(value) >= 10 and int.from_bytes(value[:2], 'little') == 11:
            return int.from_bytes(value[8:10], 'little', signed=True) != 0
    except OSError:
        pass
    return None


def _disable_listen(endpoint):
    ole32 = C.OleDLL('ole32')
    ole32.CoInitializeEx.argtypes = (C.c_void_p, C.c_uint32)
    ole32.CoInitializeEx.restype = C.c_long
    hr = ole32.CoInitializeEx(None, 2)
    initialized_here = hr != -2147417850  # RPC_E_CHANGED_MODE: use the existing apartment.
    if initialized_here:
        _check(hr, 'COM initialization')
    enumerator = C.c_void_p()
    device = C.c_void_p()
    store = C.c_void_p()
    try:
        ole32.CoCreateInstance.argtypes = (C.POINTER(GUID), C.c_void_p, C.c_uint32,
                                           C.POINTER(GUID), C.POINTER(C.c_void_p))
        ole32.CoCreateInstance.restype = C.c_long
        clsid = _guid('bcde0395-e52f-467c-8e3d-c4579291692e')
        iid = _guid('a95664d2-9614-4f35-a746-de8db63617e6')
        _check(ole32.CoCreateInstance(C.byref(clsid), None, 23, C.byref(iid), C.byref(enumerator)),
               'Audio endpoint enumeration')
        endpoint_id = '{0.0.1.00000000}.' + endpoint
        _check(_call(enumerator, 5, C.c_long, C.c_wchar_p, C.POINTER(C.c_void_p))(
            enumerator, endpoint_id, C.byref(device)), 'Finding VB-CABLE Output')
        _check(_call(device, 4, C.c_long, C.c_uint32, C.POINTER(C.c_void_p))(
            device, 2, C.byref(store)), 'Opening audio properties')
        key = PROPERTYKEY(_guid('24dbb0fc-9311-4b3d-9cf0-18ff155639d4'), 1)
        value = PROPVARIANT()
        value.vt = 11  # VT_BOOL
        value.value[0] = 0
        value.value[1] = 0
        _check(_call(store, 6, C.c_long, C.POINTER(PROPERTYKEY), C.POINTER(PROPVARIANT))(
            store, C.byref(key), C.byref(value)), 'Disabling Windows Listen')
        _check(_call(store, 7, C.c_long)(store), 'Saving audio properties')
    finally:
        for pointer in (store, device, enumerator):
            if pointer.value:
                _call(pointer, 2, C.c_ulong)(pointer)
        if initialized_here:
            ole32.CoUninitialize()


def ensure_cable_decoder_only():
    """Disable Windows speaker loopback for CABLE Output; MMSSTV capture continues."""
    state = cable_listen_enabled()
    if state is None:
        return None
    if state:
        _disable_listen(_cable_endpoint())
        if cable_listen_enabled() is not False:
            raise RuntimeError('Windows still has Listen enabled for CABLE Output')
    return state
