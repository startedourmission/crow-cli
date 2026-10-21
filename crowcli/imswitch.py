"""Temporarily switch the macOS keyboard input source to a Latin layout.

A Korean (or other CJK) input method holds a keystroke in its composition
buffer, so the terminal sends nothing until the syllable is committed. Prefix
commands therefore switch to ABC while they wait for a key and switch back
afterwards. Everything here is best-effort: on other platforms, or when the
Carbon APIs are unavailable, the functions do nothing.
"""

import ctypes
import ctypes.util
import os
import sys

_UTF8 = 0x08000100
_api = None


def _load():
    global _api
    if _api is not None:
        return _api or None
    _api = False
    if sys.platform != "darwin" or os.environ.get("CROWCLI_NO_IME_SWITCH"):
        return None
    try:
        carbon = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/Carbon.framework/Carbon")
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        vp = ctypes.c_void_p
        carbon.TISCopyCurrentKeyboardInputSource.restype = vp
        carbon.TISGetInputSourceProperty.restype = vp
        carbon.TISGetInputSourceProperty.argtypes = [vp, vp]
        carbon.TISCreateInputSourceList.restype = vp
        carbon.TISCreateInputSourceList.argtypes = [vp, ctypes.c_bool]
        carbon.TISSelectInputSource.argtypes = [vp]
        cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long,
                                          ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p,
                                                 ctypes.c_uint32]
        cf.CFDictionaryCreate.restype = vp
        cf.CFDictionaryCreate.argtypes = [vp, ctypes.POINTER(vp),
                                          ctypes.POINTER(vp), ctypes.c_long,
                                          vp, vp]
        cf.CFArrayGetCount.argtypes = [vp]
        cf.CFArrayGetValueAtIndex.restype = vp
        cf.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
        cf.CFRelease.argtypes = [vp]
        _api = {
            "carbon": carbon, "cf": cf,
            "id_key": vp.in_dll(carbon, "kTISPropertyInputSourceID").value,
            "key_cb": ctypes.addressof(
                vp.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
            "val_cb": ctypes.addressof(
                vp.in_dll(cf, "kCFTypeDictionaryValueCallBacks")),
        }
    except (OSError, ValueError, AttributeError):
        _api = False
    return _api or None


def current():
    api = _load()
    if api is None:
        return None
    src = api["carbon"].TISCopyCurrentKeyboardInputSource()
    if not src:
        return None
    sid = api["carbon"].TISGetInputSourceProperty(src, api["id_key"])
    buf = ctypes.create_string_buffer(256)
    ok = sid and api["cf"].CFStringGetCString(sid, buf, 256, _UTF8)
    api["cf"].CFRelease(src)
    return buf.value.decode() if ok else None


def select(source_id):
    api = _load()
    if api is None or not source_id:
        return False
    cf = api["cf"]
    vp = ctypes.c_void_p
    value = cf.CFStringCreateWithCString(None, source_id.encode(), _UTF8)
    keys = (vp * 1)(api["id_key"])
    vals = (vp * 1)(value)
    query = cf.CFDictionaryCreate(None, keys, vals, 1,
                                  api["key_cb"], api["val_cb"])
    found = api["carbon"].TISCreateInputSourceList(query, False)
    ok = False
    if found:
        if cf.CFArrayGetCount(found):
            src = cf.CFArrayGetValueAtIndex(found, 0)
            ok = api["carbon"].TISSelectInputSource(src) == 0
        cf.CFRelease(found)
    cf.CFRelease(query)
    cf.CFRelease(value)
    return ok


def is_composing(source_id):
    return bool(source_id) and ".inputmethod." in source_id


def to_latin():
    """Switch to ABC if an input method is active; return the ID to restore."""
    previous = current()
    if not is_composing(previous):
        return None
    for latin in ("com.apple.keylayout.ABC", "com.apple.keylayout.US"):
        if select(latin):
            return previous
    return None


def restore(source_id):
    if source_id:
        select(source_id)
