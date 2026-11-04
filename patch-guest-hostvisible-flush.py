#!/usr/bin/env python3
"""Publish the guest's CPU writes into host-visible memory before every submit.

The 2026-11-04 303104-byte image and every other small client surface reached the GPU as
zeroes, page-table leftovers or a previous frame while the guest's own copy of the same bytes
was correct. The two mappings are the same physical pages, the guest mapping is write-back
cacheable (the host advertises MAP_CACHE_CACHED for the only host-pointer-importable Mali
type), and neither process performs the CPU->GPU half of the coherence contract:

  * this driver has no on_vkFlushMappedMemoryRanges / on_vkInvalidateMappedMemoryRanges and
    on_vkUnmapMemory is a no-op, so a mapped write is never written back;
  * the host skips host-pointer imports in its vkFlushMappedMemoryRanges handling and its
    pre-submit writeback for them is opt-in (GFXSTREAM_HOSTPTR_PRESUBMIT_CLEAN), so its only
    default maintenance is the completion invalidate (GPU->CPU direction).

So the dirty lines the application filled only ever existed in cache, and the GPU read DRAM.

This patch cleans (dc cvac) every allocation the application has mapped, immediately before
the submission that may read it is encoded, and fences with dsb ish so the writebacks land
first. Guest and host address the same physical pages with physically indexed caches, which
is the same property the host's own dc cvac/civac path relies on.

Default on. GFXSTREAM_HOSTVISIBLE_FLUSH=0 turns it off for A/B; GFXSTREAM_GUEST_DIAG=1 logs
the first eight submits' allocation and byte counts.

Applied after the CIB1/GBM/ring-wake patches; none of them touch the queue submission path.
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
if 'VkResult ResourceTracker::on_vkQueueSubmitTemplate(' not in text:
    raise SystemExit(f'{REL}: on_vkQueueSubmitTemplate not found; wrong source')

OLD = '''    // The main queue submission
    input_result = vkQueueSubmitEnc(enc, queue, submitCount, prunedSubmitInfos.data(), fence);
'''

NEW = '''    // ---- CPU writes into host-visible memory must reach the GPU (HOSTVISIBLE-FLUSH) ----
    // Every HOST_VISIBLE allocation on this stack is a host-allocated SystemBlob: the host
    // imports a Mali host pointer on a cached, non-coherent memory type and advertises the
    // blob with MAP_CACHE_CACHED, so the guest kernel maps it write-back cacheable
    // (virtio_gpu_vram_mmap keeps vm_get_page_prot unless the map info is WC or uncached).
    // The application then writes pixels through that mapping and the bytes stay in dirty
    // cache lines. Nothing publishes them: this driver has no mapped-range maintenance at
    // all, a guest vkFlushMappedMemoryRanges is ignored by the host for host-pointer
    // imports, and the host's own pre-submit writeback for those imports is opt-in
    // (GFXSTREAM_HOSTPTR_PRESUBMIT_CLEAN). The GPU reads the point of coherency, so it sees
    // whatever the pages held in DRAM -- zeroes on a fresh memfd, the previous frame on a
    // reused one. Clean here, before the submission that may read the memory is encoded.
    // Guest and host address the same physical pages and the caches are physically indexed,
    // so this is exactly the writeback the GPU is missing.
    {
        std::unique_lock<std::recursive_mutex> lock(mLock);

        static const bool hostVisibleFlushEnabled = [] {
            const char* value = getenv("GFXSTREAM_HOSTVISIBLE_FLUSH");
            return !(value && value[0] == '0');
        }();

        if (hostVisibleFlushEnabled) {
            uint64_t flushedBytes = 0;
            uint32_t flushedAllocations = 0;
#if defined(__aarch64__)
            uint64_t ctr = 0;
            __asm__ __volatile__("mrs %0, ctr_el0" : "=r"(ctr));
            // CTR_EL0.DminLine is log2(words per line); 4 words is the ARMv8 minimum.
            const uintptr_t cacheLineSize = static_cast<uintptr_t>(4u) << ((ctr >> 16) & 0xfu);
#endif
            for (auto& memoryInfoIt : info_VkDeviceMemory) {
                auto& memoryInfo = memoryInfoIt.second;
                // The deferred-mapping allocation leaves ptr null until the first
                // vkMapMemory, so this is exactly the set the application can have written.
                if (!memoryInfo.ptr || !memoryInfo.allocationSize) continue;
#if defined(__aarch64__)
                // Maintenance is per line; the mapping start need not be line aligned.
                const uintptr_t begin =
                    reinterpret_cast<uintptr_t>(memoryInfo.ptr) & ~(cacheLineSize - 1);
                const uintptr_t end =
                    reinterpret_cast<uintptr_t>(memoryInfo.ptr) + memoryInfo.allocationSize;
                for (uintptr_t line = begin; line < end; line += cacheLineSize) {
                    __asm__ __volatile__("dc cvac, %0" : : "r"(line) : "memory");
                }
#else
                __builtin___clear_cache(reinterpret_cast<char*>(memoryInfo.ptr),
                                        reinterpret_cast<char*>(memoryInfo.ptr) +
                                            memoryInfo.allocationSize);
#endif
                flushedBytes += memoryInfo.allocationSize;
                ++flushedAllocations;
            }
#if defined(__aarch64__)
            if (flushedBytes) __asm__ __volatile__("dsb ish" : : : "memory");
#endif
            static uint32_t sFlushDiagCount = 0;
            if (flushedAllocations && guestDiagEnabled() && sFlushDiagCount < 8) {
                ++sFlushDiagCount;
                mesa_logw("gfxstream: HOSTVISIBLE-FLUSH: allocations=%u bytes=%llu",
                          flushedAllocations, (unsigned long long)flushedBytes);
            }
        }
    }

    // The main queue submission
    input_result = vkQueueSubmitEnc(enc, queue, submitCount, prunedSubmitInfos.data(), fence);
'''

if text.count(OLD) != 1:
    raise SystemExit(f'{REL}: expected exactly one queue submission encode site, '
                     f'found {text.count(OLD)}')
patched = text.replace(OLD, NEW, 1)

# Source-level assertions: one clean site, placed before the encode, one entry per API, the
# aarch64 sequence exactly once, and no second unconditional counter that would grow the
# per-submit walk.
if patched.count(MARKER) != 2:
    raise SystemExit(f'expected the marker twice, found {patched.count(MARKER)}')
if patched.count('__asm__ __volatile__("dc cvac, %0" : : "r"(line) : "memory");') != 1:
    raise SystemExit('expected exactly one dc cvac site')
if patched.count('__asm__ __volatile__("dsb ish" : : : "memory");') != 1:
    raise SystemExit('expected exactly one dsb ish site')
if patched.count('mrs %0, ctr_el0') != 1:
    raise SystemExit('cache line size must be read once, from CTR_EL0')
if patched.count('if (!memoryInfo.ptr || !memoryInfo.allocationSize) continue;') != 1:
    raise SystemExit('mapped-allocation guard missing or duplicated')
if patched.count('std::unique_lock<std::recursive_mutex> lock(mLock);') < 1:
    raise SystemExit('flush walk must hold mLock')
if patched.index('HOSTVISIBLE-FLUSH') > patched.index(
        'input_result = vkQueueSubmitEnc(enc, queue, submitCount, prunedSubmitInfos.data(), '
        'fence);'):
    raise SystemExit('flush block must precede the queue submission encode')
if patched.count('vkQueueSubmitEnc(enc, queue, submitCount, prunedSubmitInfos.data(), fence);') != 1:
    raise SystemExit('encode site must remain unique')
if patched.count('GFXSTREAM_HOSTVISIBLE_FLUSH') != 1:
    raise SystemExit('opt-out switch missing')
# The non-aarch64 fallback must exist so the file still builds for every target the fork
# cross-compiles; the loop body is the only architecture-specific part.
if patched.count('__builtin___clear_cache(reinterpret_cast<char*>(memoryInfo.ptr),') != 1:
    raise SystemExit('portable fallback missing')

# Balance only the block this script inserts: the rest of the file legitimately carries braces
# inside log-format strings after the earlier patches, so a file-wide count proves nothing.
inserted = patched[patched.index('HOSTVISIBLE-FLUSH') - 12:patched.index(
    '// The main queue submission')]
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

print('PASS host-visible CPU writes: dc cvac over mapped allocations before the submit encode')
print('PASS dsb ish fences the writebacks ahead of the submission')
print('PASS default on, GFXSTREAM_HOSTVISIBLE_FLUSH=0 opt-out, GFXSTREAM_GUEST_DIAG counter')
print('PASS non-aarch64 fallback and single encode site')
print('guest host-visible submit flush patched')
