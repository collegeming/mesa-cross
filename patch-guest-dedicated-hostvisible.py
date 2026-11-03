#!/usr/bin/env python3
"""Pass VkMemoryDedicatedAllocateInfo for host-visible external images.

The pinned guest ICD strips VkMemoryDedicatedAllocateInfo from every
host-visible allocation:

    #if defined(VK_USE_PLATFORM_ANDROID_KHR) || DETECT_OS_LINUX
        shouldPassThroughDedicatedAllocInfo &= !requestedMemoryIsHostVisible;
    #endif

That is correct for ordinary host-visible (coherent-emulated) memory, but it
also hides the shape of the scanout image from the host. The host coherent
waiver matches a dedicated external image to the physical allocation that
already backs it (agent A contract); with the dedicated struct stripped the
host sees shape=0 and rejects the allocation:

    COHERENT-DOWNGRADE-REJECT: ... shape=0 ... result=-8

Narrow the exclusion to "host-visible and not a dedicated external image".
The dedicated object is classified from the existing info_VkImage tracking:
only an image recorded with VkExternalMemoryImageCreateInfo (info.external,
set in on_vkCreateImage) qualifies. External buffers and non-external images
keep the original unconditional exclusion, so this patch changes exactly one
allocation class: host-visible memory dedicated to an external image.

Applied after the existing CIB1/GBM ResourceTracker patches; the allocation
branches those patches rewrite do not contain this anchor.
"""
import argparse
import difflib
from pathlib import Path

REL = 'src/gfxstream/guest/vulkan_enc/ResourceTracker.cpp'
MARKER = 'hostVisibleExternalImageDedicated'

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
parser.add_argument('--diff', type=Path)
args = parser.parse_args()
root = args.root.resolve()

rt = root / REL
text = rt.read_text(encoding='utf-8')

if MARKER in text:
    raise SystemExit(f'{REL}: already patched ({MARKER} present)')
if 'dedicatedAllocInfoPtr' not in text:
    raise SystemExit(f'{REL}: dedicatedAllocInfoPtr not found; wrong source')

OLD = '''#if defined(VK_USE_PLATFORM_ANDROID_KHR) || DETECT_OS_LINUX
    shouldPassThroughDedicatedAllocInfo &= !requestedMemoryIsHostVisible;
#endif  // VK_USE_PLATFORM_FUCHSIA'''

NEW = '''#if defined(VK_USE_PLATFORM_ANDROID_KHR) || DETECT_OS_LINUX
    // Host-visible memory types belong to the guest coherent-memory emulation, so their
    // VkMemoryDedicatedAllocateInfo is normally stripped before reaching the host. The one
    // exception is a host-visible allocation dedicated to an EXTERNAL image: the scanout
    // path needs the image shape on the host so the coherent waiver can match it to the
    // allocation that already backs it. External buffers and non-external images keep the
    // original unconditional exclusion.
    bool hostVisibleExternalImageDedicated = false;
    if (dedicatedAllocInfoPtr && dedicatedAllocInfoPtr->image != VK_NULL_HANDLE) {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        auto dedicatedImage = info_VkImage.find(dedicatedAllocInfoPtr->image);
        hostVisibleExternalImageDedicated =
            dedicatedImage != info_VkImage.end() && dedicatedImage->second.external &&
            dedicatedImage->second.device == device;
    }
    shouldPassThroughDedicatedAllocInfo &=
        !requestedMemoryIsHostVisible || hostVisibleExternalImageDedicated;
#endif  // VK_USE_PLATFORM_FUCHSIA'''

if text.count(OLD) != 1:
    raise SystemExit(f'{REL}: expected one dedicated host-visible exclusion, '
                     f'found {text.count(OLD)}')
patched = text.replace(OLD, NEW, 1)

# Source-level assertions: the new predicate is present exactly once and the
# old unconditional exclusion is gone. A bare grep for the new identifier and
# for the old expression runs again in the workflow.
if patched.count(MARKER) != 3:
    raise SystemExit(f'expected the new predicate three times, found {patched.count(MARKER)}')
if patched.count('shouldPassThroughDedicatedAllocInfo &= !requestedMemoryIsHostVisible;') != 0:
    raise SystemExit('old unconditional host-visible exclusion still present')
if patched.count('auto dedicatedImage = info_VkImage.find(dedicatedAllocInfoPtr->image);') != 1:
    raise SystemExit('dedicated image lookup missing or duplicated')
if patched.count('!requestedMemoryIsHostVisible || hostVisibleExternalImageDedicated;') != 1:
    raise SystemExit('narrowed exclusion expression missing')
if 'dedicatedImage->second.external' not in patched:
    raise SystemExit('external image requirement missing')

if args.diff:
    args.diff.write_text(
        ''.join(difflib.unified_diff(text.splitlines(True), patched.splitlines(True),
                                     fromfile='a/' + REL, tofile='b/' + REL)),
        encoding='utf-8', newline='\n')
rt.write_text(patched, encoding='utf-8', newline='')

print('PASS host-visible dedicated image: VkMemoryDedicatedAllocateInfo passed through')
print('PASS external buffer / non-external image / other host-visible: exclusion unchanged')
print('PASS old unconditional `&= !requestedMemoryIsHostVisible` removed')
print('guest dedicated host-visible external image patched')
