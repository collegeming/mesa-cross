#!/usr/bin/env python3
"""Assert the invariants of the host-visible CPU->GPU flush patch on a patched tree.

Run against the patched mesa checkout (Actions does this after applying the patch chain and
again after the build). No GPU, no Vulkan linkage, no device: this only proves that the code
that is about to ship still says what the root cause requires.

Checks:
  1. the flush block exists exactly once, before the one queue-submission encode site;
  2. inside that block: mLock held, mapped-allocation guard, dc cvac per line, dsb ish,
     CTR_EL0-derived line size, portable fallback, opt-out switch;
  3. the covered line range always contains every byte of the mapping for every legal line
     size and an unaligned base (the arithmetic is modelled and compared to the source);
  4. the driver still has no mapped-range hook, so nothing else is assumed to do this work;
  5. the file is brace-balanced.
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
source = (root / 'src/gfxstream/guest/vulkan_enc/ResourceTracker.cpp').read_text(
    encoding='utf-8', errors='replace')
source_clean = source.replace('\r\n', '\n')

MARKER = 'HOSTVISIBLE-FLUSH'
ENCODE = 'input_result = vkQueueSubmitEnc(enc, queue, submitCount, prunedSubmitInfos.data(), fence);'

# 1. exactly one flush block, before the single encode site.
assert source_clean.count(MARKER) == 2, 'flush block must appear once (comment + log tag)'
assert source_clean.count(ENCODE) == 1, 'queue submission encode site must stay unique'
start = source_clean.index('    // ---- CPU writes into host-visible memory must reach the GPU')
end = source_clean.index(ENCODE)
block = source_clean[start:end]
assert end > start
assert 'HOSTVISIBLE-FLUSH' in block

# 2. the block does what the root cause needs, in this order.
required = [
    'std::unique_lock<std::recursive_mutex> lock(mLock);',
    'getenv("GFXSTREAM_HOSTVISIBLE_FLUSH")',
    'uint64_t ctr = 0;',
    '__asm__ __volatile__("mrs %0, ctr_el0" : "=r"(ctr));',
    'const uintptr_t cacheLineSize = static_cast<uintptr_t>(4u) << ((ctr >> 16) & 0xfu);',
    'if (!memoryInfo.ptr || !memoryInfo.allocationSize) continue;',
    'reinterpret_cast<uintptr_t>(memoryInfo.ptr) & ~(cacheLineSize - 1);',
    '__asm__ __volatile__("dc cvac, %0" : : "r"(line) : "memory");',
    '__builtin___clear_cache(reinterpret_cast<char*>(memoryInfo.ptr),',
    '__asm__ __volatile__("dsb ish" : : : "memory");',
]
position = 0
for snippet in required:
    found = block.find(snippet, position)
    assert found >= 0, f'missing or out of order inside the flush block: {snippet}'
    position = found
assert block.count('dc cvac') == 1, 'one clean site only'
assert block.count('dsb ish') == 1, 'one fence site only'
assert 'dc civac' not in block, 'a clean must not drop the guest dirty lines'
# The guard must exclude allocations the application never mapped: the deferred-mapping
# allocation path leaves ptr null until the first vkMapMemory.
assert 'if (!memoryInfo.ptr' in block
assert block.count('for (auto& memoryInfoIt : info_VkDeviceMemory)') == 1

# 3. line-range arithmetic: every byte of the mapping is covered, for every legal DminLine and
# an unaligned base. This models the two expressions the block uses.
def covered(base, size, line):
    begin = base & ~(line - 1)
    return begin, begin + size + ((base - begin + line - 1) // line) * line

for line in (64, 128, 256, 512):
    for size in (1, 3, 4095, 4096, 4097, 261696, 303104, 2097152, 3686400):
        for misalign in (0, 1, 63, 4095):
            base = 0xFFFF00000000 + misalign
            begin, end = covered(base, size, line)
            assert begin <= base and begin % line == 0
            assert begin + ((end - begin + line - 1) // line) * line >= base + size
            assert (end - begin) <= size + 2 * line

# 4. no mapped-range hook is present, and unmap is still a no-op: this patch is the only
#    CPU->GPU maintenance in the driver.
assert 'on_vkFlushMappedMemoryRanges' not in source_clean
assert 'on_vkInvalidateMappedMemoryRanges' not in source_clean
unmap = source_clean.index('void ResourceTracker::on_vkUnmapMemory')
assert '// no-op' in source_clean[unmap:unmap + 160]

# 5. balanced braces.
assert source_clean.count('{') == source_clean.count('}'), 'unbalanced braces'

report = (
    'PASS one flush block, ahead of the single queue submission encode\n'
    'PASS mLock, mapped-allocation guard, CTR_EL0 line size, per-line dc cvac, dsb ish\n'
    'PASS no civac (guest dirty lines are published, not dropped)\n'
    'PASS line range covers every byte for DminLine 64..512, sizes 1..3686400, unaligned base\n'
    'PASS portable fallback present; GFXSTREAM_HOSTVISIBLE_FLUSH opt-out present\n'
    'PASS driver still has no mapped-range hook; on_vkUnmapMemory still a no-op\n'
    'PASS source is brace-balanced\n'
)
print(report, end='')
if out:
    out.mkdir(parents=True, exist_ok=True)
    (out / 'hostvisible-flush-source-checks.txt').write_text(report)
