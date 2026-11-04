#!/usr/bin/env python3
"""v2: make GPU writes visible to the application that maps host-visible memory for reading.

Companion to patch-guest-hostvisible-flush.py (which publishes the CPU->GPU direction at
vkUnmapMemory). This one covers GPU->CPU at vkMapMemory.

The host type is cached/non-coherent, Mali's writes reach the point of coherency rather than
the CPU's cached lines, and the host's completion invalidate is best effort -- once 64
submissions are pending it drops the record and that submission "gets no cache maintenance".
This driver performs no invalidate at all, so a mapped readback can return the previous frame
or zeroes. A niri-native screenshot is exactly that shape: render, glReadPixels into a
pixel-pack buffer, map it, read it. It is also the only full-frame CPU boundary in the source
stack -- smithay's GLES renderer uploads its vertex buffers once at creation and sends
per-element data as uniforms -- which is why a single garbage client texture cannot paint the
whole output but a garbage readback can.

`dc civac` cleans before dropping, so lines the application itself wrote through this mapping
are written back rather than lost; `dc ivac` would drop them and is never used.

Default OFF and independent of the flush switch, so one build can A/B each direction:
  GFXSTREAM_HOSTVISIBLE_INVALIDATE=1   enable
  GFXSTREAM_HOSTVISIBLE_FLUSH_MAX_MB   per-mapping cap (default 64), over-cap is skipped
  GFXSTREAM_GUEST_DIAG=1               first eight invalidates with counters

Applied on top of patch-guest-hostvisible-flush.py; the anchors do not overlap.
"""
import argparse
import difflib
from pathlib import Path

REL = 'src/gfxstream/guest/vulkan_enc/ResourceTracker.cpp'
MARKER = 'HOSTVISIBLE-INVALIDATE'

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
parser.add_argument('--diff', type=Path)
args = parser.parse_args()
root = args.root.resolve()

rt = root / REL
text = rt.read_text(encoding='utf-8')
text = text.replace('\r\n', '\n')

if MARKER in text:
    raise SystemExit(f'{REL}: already patched ({MARKER} present)')

OLD = '''    *ppData = deviceMemoryInfo.ptr + offset;

    return host_result;
'''

NEW = '''    *ppData = deviceMemoryInfo.ptr + offset;

    // ---- GPU writes must be visible to the CPU that is about to read (HOSTVISIBLE-INVALIDATE) ----
    // The other half of the contract the unmap-time clean publishes. Same cached, non-coherent
    // host type; Mali's writes are not in the CPU's cached lines, and the host's completion
    // invalidate is best effort (dropped once 64 submissions are pending). Without this a mapped
    // readback -- a compositor screenshot is render + glReadPixels into a PBO + map -- can return
    // the previous frame or zeroes. dc civac cleans before dropping, so the application's own
    // dirty lines are written back, not lost. Bounded to the allocation being mapped.
    static const bool hostVisibleInvalidateEnabled = [] {
        const char* value = getenv("GFXSTREAM_HOSTVISIBLE_INVALIDATE");
        return value && value[0] == '1';
    }();
    if (hostVisibleInvalidateEnabled && deviceMemoryInfo.ptr && deviceMemoryInfo.allocationSize) {
        static const uint64_t hostVisibleInvalidateMaxBytes = []() -> uint64_t {
            const char* value = getenv("GFXSTREAM_HOSTVISIBLE_FLUSH_MAX_MB");
            uint64_t megabytes = 64;
            if (value && value[0] >= '0' && value[0] <= '9') {
                uint64_t parsed = 0;
                for (const char* p = value; *p >= '0' && *p <= '9'; ++p) {
                    parsed = parsed * 10 + static_cast<uint64_t>(*p - '0');
                    if (parsed > 4096) break;
                }
                if (parsed) megabytes = parsed;
            }
            return megabytes * 1024ull * 1024ull;
        }();

        static uint64_t sInvalidates = 0;
        static uint64_t sInvalidatedBytes = 0;
        static uint64_t sInvalidateOverCap = 0;
        static uint32_t sInvalidateDiagCount = 0;

        const uint64_t invalidateSize = deviceMemoryInfo.allocationSize;
        if (invalidateSize > hostVisibleInvalidateMaxBytes) {
            ++sInvalidateOverCap;
        } else {
#if defined(__aarch64__)
            uint64_t ctr = 0;
            __asm__ __volatile__("mrs %0, ctr_el0" : "=r"(ctr));
            const uintptr_t cacheLineSize = static_cast<uintptr_t>(4u) << ((ctr >> 16) & 0xfu);
            const uintptr_t begin =
                reinterpret_cast<uintptr_t>(deviceMemoryInfo.ptr) & ~(cacheLineSize - 1);
            const uintptr_t end =
                reinterpret_cast<uintptr_t>(deviceMemoryInfo.ptr) + invalidateSize;
            for (uintptr_t line = begin; line < end; line += cacheLineSize) {
                __asm__ __volatile__("dc civac, %0" : : "r"(line) : "memory");
            }
            __asm__ __volatile__("dsb ish" : : : "memory");
#else
            __builtin___clear_cache(reinterpret_cast<char*>(deviceMemoryInfo.ptr),
                                    reinterpret_cast<char*>(deviceMemoryInfo.ptr) +
                                        invalidateSize);
#endif
            ++sInvalidates;
            sInvalidatedBytes += invalidateSize;
        }

        if (guestDiagEnabled() && sInvalidateDiagCount < 8) {
            ++sInvalidateDiagCount;
            mesa_logw("gfxstream: HOSTVISIBLE-INVALIDATE: invalidates=%llu bytes=%llu "
                      "over-cap=%llu cap=%llu",
                      (unsigned long long)sInvalidates,
                      (unsigned long long)sInvalidatedBytes,
                      (unsigned long long)sInvalidateOverCap,
                      (unsigned long long)hostVisibleInvalidateMaxBytes);
        }
    }

    return host_result;
'''

if text.count(OLD) != 1:
    raise SystemExit(f'{REL}: expected exactly one vkMapMemory pointer return, '
                     f'found {text.count(OLD)}')
patched = text.replace(OLD, NEW, 1)

# Invariants.
if patched.count(MARKER) != 2:
    raise SystemExit(f'expected the marker twice, found {patched.count(MARKER)}')
if patched.count('__asm__ __volatile__("dc civac, %0" : : "r"(line) : "memory");') != 1:
    raise SystemExit('expected exactly one dc civac site')
if patched.count('__asm__ __volatile__("dsb ish" : : : "memory");') != 2:
    raise SystemExit('both directions must fence with dsb ish')
if 'dc ivac' in patched:
    raise SystemExit('dc ivac would drop unflushed guest writes; civac is required')
if patched.count("value[0] == '1'") != 2:
    raise SystemExit('both directions must be opt-in (separate =1 switches)')
if patched.count('GFXSTREAM_HOSTVISIBLE_INVALIDATE') != 1:
    raise SystemExit('invalidate switch missing')
if patched.count('invalidateSize > hostVisibleInvalidateMaxBytes') != 1:
    raise SystemExit('invalidate cap must be enforced before the walk')
if patched.count('deviceMemoryInfo.ptr && deviceMemoryInfo.allocationSize') != 1:
    raise SystemExit('invalidate must refuse a missing or empty mapping')
index_invalidate = patched.index(MARKER)
if index_invalidate < patched.index('VkResult ResourceTracker::on_vkMapMemory('):
    raise SystemExit('invalidate block must live inside on_vkMapMemory')
if index_invalidate > patched.index('void ResourceTracker::on_vkUnmapMemory('):
    raise SystemExit('invalidate block must precede on_vkUnmapMemory')

# Only the inserted block is checked for balance (see the flush patch for why).
block_end = patched.index('\n    return host_result;\n}\n\nvoid ResourceTracker::on_vkUnmapMemory')
inserted = patched[patched.index(MARKER) - 12:block_end]
open_braces = inserted.count('{')
close_braces = inserted.count('}')
if open_braces != close_braces:
    raise SystemExit(f'inserted invalidate block is unbalanced ({open_braces} open, '
                     f'{close_braces} close)')

if args.diff:
    args.diff.write_text(
        ''.join(difflib.unified_diff(text.splitlines(True), patched.splitlines(True),
                                     fromfile='a/' + REL, tofile='b/' + REL)),
        encoding='utf-8', newline='\n')
rt.write_text(patched, encoding='utf-8', newline='')

print('PASS GPU->CPU: dc civac over the just-mapped allocation, before the caller reads it')
print("PASS civac not ivac: the dirty lines the application owns are written back, not dropped")
print('PASS opt-in: GFXSTREAM_HOSTVISIBLE_INVALIDATE=1, independent of the flush switch')
print('PASS per-mapping cap with over-cap counter; dsb ish after the walk')
print('guest host-visible map invalidate v2 patched')
