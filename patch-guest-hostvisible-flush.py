#!/usr/bin/env python3
"""v2: publish host-visible CPU writes to the GPU when the application unmaps the memory.

v1 cleaned every mapped host-visible allocation on every queue submission. It fixed nothing
it was measured on and it broke the device test: niri died (status=11/SEGV) ~11 s in, right
after `no reply to opcode 20061` (OP_vkCreatePipelineCache) on the synchronous stream, and the
screen went black. Two concrete defects in v1, both fixed here:

  * the submit-time sweep walked allocations that were only bookkept as mapped. The tracker
    keeps VkDeviceMemory_Info entries after vkFreeMemory (only on_vkDestroyDevice_pre erases
    them) and clears ptr only on the coherent free path, so a walk keyed on `ptr != nullptr`
    is one bookkeeping slip away from `dc cvac` on an unmapped VA -- a SEGV at EL0.
  * a full sweep per submit is unbounded work inside the submit path: 15 MB of cache lines is
    ~230k `dc cvac`, and on a VA whose translation is the virtio BAR the maintenance cost is
    far above an L2 hit, so the submit side can hold the shared tracker lock long enough that
    a synchronous call's reply is missed. That is what 20061 (CreatePipelineCache) looked
    like: not a submit, a small synchronous call that starved behind the sweep.

v2 cleans once, at vkUnmapMemory, over exactly the allocation the application just finished
writing, with the tracker lock held briefly and only around that one allocation. It runs
before anything can free the memory -- vkFreeMemory requires the memory not be mapped -- so a
stale VA is impossible by construction, and the per-submit cost is zero.

Default OFF. Set GFXSTREAM_HOSTVISIBLE_FLUSH=1 to enable; GFXSTREAM_HOSTVISIBLE_FLUSH_MAX_MB
(default 64) caps a single walk, and allocations over it are counted as skipped instead of
walked. GFXSTREAM_GUEST_DIAG=1 logs the first eight cleanups with flush/byte/skip counters, so
a device run shows whether uploads even take the map/unmap path.
"""
import argparse
import difflib
from pathlib import Path

REL = 'src/gfxstream/guest/vulkan_enc/ResourceTracker.cpp'
MARKER = 'HOSTVISIBLE-FLUSH'

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

OLD = '''void ResourceTracker::on_vkUnmapMemory(void*, VkDevice, VkDeviceMemory) {
    // no-op
}
'''

NEW = '''void ResourceTracker::on_vkUnmapMemory(void*, VkDevice, VkDeviceMemory memory) {
    // ---- CPU writes into host-visible memory must reach the GPU (HOSTVISIBLE-FLUSH, v2) ----
    // Every HOST_VISIBLE allocation on this stack is a host-allocated SystemBlob imported as a
    // Mali host pointer on a cached, non-coherent type, and the host advertises MAP_CACHE_CACHED,
    // so the guest kernel maps it write-back cacheable (virtio_gpu_vram_mmap leaves vm_get_prot
    // alone unless the map info is WC/uncached). The application writes through that mapping and
    // the bytes stay in dirty cache lines; nothing publishes them (no mapped-range hook here, the
    // host ignores flushes for host-pointer imports, and its pre-submit writeback is opt-in), so
    // the GPU reads the point of coherency: zeroes on a fresh memfd, the previous frame on a
    // reused one.
    //
    // Clean at unmap, not per submit. The application has finished writing at this point, the
    // memory cannot be freed until it is unmapped (VUID-vkFreeMemory-memory-00677), and the walk
    // is bounded to the one allocation it just used. v1 swept every mapped allocation on every
    // submission while holding the shared tracker lock; that is unbounded work in the submit
    // path and can starve a synchronous call on the type-1 stream.
    static const bool hostVisibleFlushEnabled = [] {
        const char* value = getenv("GFXSTREAM_HOSTVISIBLE_FLUSH");
        return value && value[0] == '1';
    }();
    if (!hostVisibleFlushEnabled) return;

    static const uint64_t hostVisibleFlushMaxBytes = []() -> uint64_t {
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

    static uint64_t sFlushes = 0;
    static uint64_t sBytes = 0;
    static uint64_t sSkippedUnmapped = 0;
    static uint64_t sSkippedOverCap = 0;
    static uint32_t sDiagCount = 0;

    {
        // Only this one allocation, and only while its mapping is still alive.
        std::lock_guard<std::recursive_mutex> lock(mLock);

        auto it = info_VkDeviceMemory.find(memory);
        uint8_t* ptr = (it == info_VkDeviceMemory.end()) ? nullptr : it->second.ptr;
        const uint64_t size = (it == info_VkDeviceMemory.end()) ? 0 : it->second.allocationSize;
        if (!ptr || !size) {
            ++sSkippedUnmapped;
        } else if (size > hostVisibleFlushMaxBytes) {
            ++sSkippedOverCap;
        } else {
#if defined(__aarch64__)
            uint64_t ctr = 0;
            __asm__ __volatile__("mrs %0, ctr_el0" : "=r"(ctr));
            // CTR_EL0.DminLine is log2(words per line); 4 words is the ARMv8 minimum.
            const uintptr_t cacheLineSize = static_cast<uintptr_t>(4u) << ((ctr >> 16) & 0xfu);
            // Maintenance is per line; the mapping start need not be line aligned.
            const uintptr_t begin = reinterpret_cast<uintptr_t>(ptr) & ~(cacheLineSize - 1);
            const uintptr_t end = reinterpret_cast<uintptr_t>(ptr) + size;
            for (uintptr_t line = begin; line < end; line += cacheLineSize) {
                __asm__ __volatile__("dc cvac, %0" : : "r"(line) : "memory");
            }
            __asm__ __volatile__("dsb ish" : : : "memory");
#else
            __builtin___clear_cache(reinterpret_cast<char*>(ptr),
                                    reinterpret_cast<char*>(ptr) + size);
#endif
            ++sFlushes;
            sBytes += size;
        }

        if (guestDiagEnabled() && sDiagCount < 8) {
            ++sDiagCount;
            mesa_logw("gfxstream: HOSTVISIBLE-FLUSH: flushes=%llu bytes=%llu unmapped=%llu "
                      "over-cap=%llu cap=%llu",
                      (unsigned long long)sFlushes, (unsigned long long)sBytes,
                      (unsigned long long)sSkippedUnmapped, (unsigned long long)sSkippedOverCap,
                      (unsigned long long)hostVisibleFlushMaxBytes);
        }
    }
}
'''

if text.count(OLD) != 1:
    raise SystemExit(f'{REL}: expected exactly one on_vkUnmapMemory no-op, found {text.count(OLD)}')
patched = text.replace(OLD, NEW, 1)

# Invariants of the v2 shape.
if patched.count(MARKER) != 2:
    raise SystemExit(f'expected the marker twice, found {patched.count(MARKER)}')
if patched.count('__asm__ __volatile__("dc cvac, %0" : : "r"(line) : "memory");') != 1:
    raise SystemExit('expected exactly one dc cvac site')
if patched.count('__asm__ __volatile__("dsb ish" : : : "memory");') != 1:
    raise SystemExit('expected exactly one dsb ish site')
if patched.count('value[0] == \'1\'') != 1:
    raise SystemExit('the flush must be opt-in: only GFXSTREAM_HOSTVISIBLE_FLUSH=1 enables it')
if patched.count('GFXSTREAM_HOSTVISIBLE_FLUSH_MAX_MB') != 1:
    raise SystemExit('the per-allocation cap is missing')
if patched.count('size > hostVisibleFlushMaxBytes') != 1:
    raise SystemExit('the cap must be enforced before the walk')
if patched.count('if (!ptr || !size) {') != 1:
    raise SystemExit('the walk must refuse a missing or empty allocation')
if patched.count('std::lock_guard<std::recursive_mutex> lock(mLock);') < 1:
    raise SystemExit('the walk must hold mLock')
# v2 must not touch the submission path at all: that is what v1 broke.
if 'dc cvac' in patched[patched.index('VkResult ResourceTracker::on_vkQueueSubmitTemplate('):
                         patched.index('VkResult ResourceTracker::on_vkQueueWaitIdle(')]:
    raise SystemExit('the submission path must not carry cache maintenance (v1 regression)')
if 'vkQueueSubmitEnc' not in patched:
    raise SystemExit('submission encode site disappeared; wrong file')

# Only the inserted block is checked for balance: the earlier patches in this chain carry
# braces inside log-format strings, so a file-wide count proves nothing.
start = patched.index('void ResourceTracker::on_vkUnmapMemory(')
end = patched.index('void ResourceTracker::transformImageMemoryRequirements2ForGuest(')
inserted = patched[start:end]
open_braces = inserted.count('{')
close_braces = inserted.count('}')
if open_braces != close_braces:
    raise SystemExit(f'inserted flush block is unbalanced ({open_braces} open, '
                     f'{close_braces} close)')

if args.diff:
    args.diff.write_text(
        ''.join(difflib.unified_diff(text.splitlines(True), patched.splitlines(True),
                                     fromfile='a/' + REL, tofile='b/' + REL)),
        encoding='utf-8', newline='\n')
rt.write_text(patched, encoding='utf-8', newline='')

print('PASS flush at vkUnmapMemory over one live allocation, tracker lock held, then released')
print('PASS opt-in only: GFXSTREAM_HOSTVISIBLE_FLUSH=1; per-allocation cap with skipped counter')
print('PASS no cache maintenance anywhere on the submission path')
print('PASS dc cvac + dsb ish, one site each; counters for flushes/bytes/unmapped/over-cap')
print('guest host-visible unmap flush v2 patched')
