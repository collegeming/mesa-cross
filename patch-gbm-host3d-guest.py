#!/usr/bin/env python3
"""Force host-visible exportable allocations to HOST3D + MAPPABLE when requested.

The compositor's scanout buffer is allocated through zink -> gfxstream ICD. By
default an exportable HOST_VISIBLE allocation takes the GUEST +
CREATE_GUEST_HANDLE branch: the VMM gives gfxstream no handle for that blob and
its memory can never be the host ColorBuffer that renders. With
GFXSTREAM_GBM_HOST3D=1 the same allocation is created as a HOST3D blob, for
which the host allocates a ColorBuffer; the image import can then bind to that
same ColorBuffer memory.

Applied after the CIB1 guest patch; anchors are the allocation branches in the
export/dmabuf path, which the bridge patch does not rewrite.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

rt = root / 'src/gfxstream/guest/vulkan_enc/ResourceTracker.cpp'
text = rt.read_text(encoding='utf-8')

anchor = '''static bool guestDiagEnabled() {
    static const bool enabled = [] {
        const char* v = getenv("GFXSTREAM_GUEST_DIAG");
        return v && v[0] && strcmp(v, "0") != 0;
    }();
    return enabled;
}
'''
helper = '''
static bool gfxstreamGbmHost3d() {
    static const bool enabled = [] {
        const char* v = getenv("GFXSTREAM_GBM_HOST3D");
        return v && v[0] && strcmp(v, "0") != 0;
    }();
    return enabled;
}
'''
if text.count(anchor) != 1:
    raise SystemExit(f'guestDiagEnabled anchor count {text.count(anchor)}')
if 'gfxstreamGbmHost3d' not in text:
    text = text.replace(anchor, anchor + helper, 1)

# 1) The two inner host-visible guest-handle branches at 16 spaces. Replace
#    these before the 12-space form: a 16-space line contains the 12-space
#    pattern starting at offset 4.
old = ('                if (requestedMemoryIsHostVisible && '
       'mCaps.params[kParamCreateGuestHandle]) {')
new = ('                if (!gfxstreamGbmHost3d() && requestedMemoryIsHostVisible &&\n'
       '                    mCaps.params[kParamCreateGuestHandle]) {')
if text.count(old) != 2:
    raise SystemExit(f'inner host-visible anchor count {text.count(old)}')
text = text.replace(old, new)

# 2) Host-visible guest-handle branch at 12 spaces (dedicated image path).
old = ('            if (requestedMemoryIsHostVisible && '
       'mCaps.params[kParamCreateGuestHandle]) {')
new = ('            if (!gfxstreamGbmHost3d() && requestedMemoryIsHostVisible &&\n'
       '                mCaps.params[kParamCreateGuestHandle]) {')
if text.count(old) != 1:
    raise SystemExit(f'image host-visible anchor count {text.count(old)}')
text = text.replace(old, new, 1)

# 3) Enter the always-blob (3D resource) path even when the capset says no.
old = '            } else if (mCaps.vulkanCapset.alwaysBlob) {'
new = '            } else if (mCaps.vulkanCapset.alwaysBlob || gfxstreamGbmHost3d()) {'
if text.count(old) != 1:
    raise SystemExit(f'else-alwaysBlob anchor count {text.count(old)}')
text = text.replace(old, new, 1)

old = '            if (mCaps.vulkanCapset.alwaysBlob) {'
new = '            if (mCaps.vulkanCapset.alwaysBlob || gfxstreamGbmHost3d()) {'
if text.count(old) != 1:
    raise SystemExit(f'alwaysBlob anchor count {text.count(old)}')
text = text.replace(old, new, 1)

# 4) HOST3D blobs need MAPPABLE so the guest can map them.
old = '                createBlob.flags = kBlobFlagShareable | kBlobFlagCrossDevice;'
new = ('                createBlob.flags = static_cast<VirtGpuResourceFlags>(\n'
       '                    kBlobFlagShareable | kBlobFlagCrossDevice |\n'
       '                    (gfxstreamGbmHost3d() ? kBlobFlagMappable : 0));')
if text.count(old) != 2:
    raise SystemExit(f'host3d flags anchor count {text.count(old)}')
text = text.replace(old, new)

# 5) Page-align the image blob size when forcing HOST3D.
old = '                createBlob.size = finalAllocInfo.allocationSize;'
new = ('                createBlob.size = gfxstreamGbmHost3d()\n'
       '                                      ? ALIGN_POT(finalAllocInfo.allocationSize, 4096)\n'
       '                                      : finalAllocInfo.allocationSize;')
if text.count(old) != 1:
    raise SystemExit(f'host3d size anchor count {text.count(old)}')
text = text.replace(old, new, 1)

rt.write_text(text, encoding='utf-8', newline='')
print('GBM HOST3D allocation patched')
