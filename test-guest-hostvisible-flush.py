#!/usr/bin/env python3
"""Assert the invariants of the v2 host-visible flush patch (clean at vkUnmapMemory).

v1 swept every mapped allocation on every submission and broke the device test (SEGV after
`no reply to opcode 20061`, OP_vkCreatePipelineCache). The invariants below are the ones that
keep that shape out of the shipped source.

Checks:
  1. the flush lives only in on_vkUnmapMemory and there is no cache maintenance anywhere on
     the submission path;
  2. default OFF: the switch is read as `== '1'` and nothing runs before that check;
  3. destroyed/allocation safety: the walk looks the memory up in info_VkDeviceMemory under
     mLock, refuses a missing entry or a null pointer, and is bounded by that allocation;
  4. a hard per-allocation cap with a skipped counter exists before the walk;
  5. one dc cvac site and one dsb ish site, both inside the guarded branch;
  6. counters for flushes / bytes / unmapped / over-cap are logged once per submit window;
  7. the inserted function is brace-balanced.
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
source = (root / 'src/gfxstream/guest/vulkan_enc/ResourceTracker.cpp').read_text(
    encoding='utf-8', errors='replace').replace('\r\n', '\n')

UNMAP = 'void ResourceTracker::on_vkUnmapMemory('
SUBMIT = 'VkResult ResourceTracker::on_vkQueueSubmitTemplate('
WAIT = 'VkResult ResourceTracker::on_vkQueueWaitIdle('
MARKER = 'HOSTVISIBLE-FLUSH'

unmap_start = source.index(UNMAP)
unmap_end = source.index('void ResourceTracker::transformImageMemoryRequirements2ForGuest(')
unmap = source[unmap_start:unmap_end]

# 1. the flush is not on the submission path.
submit_body = source[source.index(SUBMIT):source.index(WAIT)]
assert 'dc cvac' not in submit_body, 'submission path must not carry cache maintenance (v1 bug)'
assert 'HOSTVISIBLE-FLUSH' not in submit_body, 'submission path must not carry the flush'
assert unmap.count(MARKER) == 2, 'flush must appear once (comment + log tag)'

# 2. default off, checked before anything else in the function.
assert "value && value[0] == '1'" in unmap, "opt-in must be exactly `value[0] == '1'`"
gate = unmap.index('if (!hostVisibleFlushEnabled) return;')
assert gate < unmap.index('std::lock_guard<std::recursive_mutex> lock(mLock);')
assert gate < unmap.index('dc cvac')
assert 'hostVisibleFlushEnabled = true' not in unmap

# The truth table the shipped predicate implements, modelled here.
def enabled(value):
    return bool(value) and value[0] == '1'
for value, expected in ((None, False), ('', False), ('0', False), ('1', True), ('1x', True),
                        ('true', False), ('yes', False)):
    assert enabled(value) is expected, f'opt-in truth table wrong for {value!r}'

# 3. allocation safety: lookup under the lock, refuse missing or null, bound by the allocation.
for snippet in (
    'std::lock_guard<std::recursive_mutex> lock(mLock);',
    'auto it = info_VkDeviceMemory.find(memory);',
    'if (!ptr || !size) {',
    'const uint64_t size = (it == info_VkDeviceMemory.end()) ? 0 : it->second.allocationSize;',
):
    assert snippet in unmap, f'missing allocation-safety invariant: {snippet}'
assert unmap.index('info_VkDeviceMemory.find(memory)') < unmap.index('dc cvac'), \
    'the walk must look the allocation up before touching its pointer'
assert 'getMappedPointer(' not in unmap, 'must not use a cached pointer accessor'

# 4. cap before the walk, with a counter.
assert 'size > hostVisibleFlushMaxBytes' in unmap
assert unmap.index('size > hostVisibleFlushMaxBytes') < unmap.index('dc cvac')
assert 'GFXSTREAM_HOSTVISIBLE_FLUSH_MAX_MB' in unmap
assert '++sSkippedOverCap;' in unmap

# 5. exactly one clean site and one fence, after the guards.
assert unmap.count('__asm__ __volatile__("dc cvac, %0" : : "r"(line) : "memory");') == 1
assert unmap.count('__asm__ __volatile__("dsb ish" : : : "memory");') == 1
assert unmap.index('size > hostVisibleFlushMaxBytes') < unmap.index('dsb ish')
assert '__builtin___clear_cache(' in unmap, 'portable fallback missing'

# 6. counters.
for counter in ('sFlushes', 'sBytes', 'sSkippedUnmapped', 'sSkippedOverCap'):
    assert counter in unmap, f'missing counter {counter}'
assert 'flushes=%llu bytes=%llu unmapped=%llu' in unmap
assert 'guestDiagEnabled()' in unmap

# 7. balance of the whole rewritten function.
assert unmap.count('{') == unmap.count('}'), 'on_vkUnmapMemory is unbalanced'
# The patch must not have removed the one submission encode site.
assert source.count('vkQueueSubmitEnc(enc, queue, submitCount, prunedSubmitInfos.data(), fence);') == 1

report = (
    'PASS flush only in on_vkUnmapMemory; submission path carries no cache maintenance\n'
    'PASS default OFF (`value[0] == \'1\'`), gate precedes lock/walk; truth table modelled\n'
    'PASS allocation safety: mLock + info lookup + null/empty refusal + allocation bound\n'
    'PASS per-allocation cap before the walk with over-cap counter\n'
    'PASS one dc cvac + one dsb ish, after the guards; portable fallback present\n'
    'PASS counters flushes/bytes/unmapped/over-cap under GFXSTREAM_GUEST_DIAG\n'
    'PASS on_vkUnmapMemory brace-balanced; submission encode site intact\n'
)
print(report, end='')
if out:
    out.mkdir(parents=True, exist_ok=True)
    (out / 'hostvisible-flush-source-checks.txt').write_text(report)
