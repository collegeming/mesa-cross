#!/usr/bin/env python3
"""Assert the invariants of the v2 GPU->CPU invalidate patch (invalidate at vkMapMemory).

Checks:
  1. the invalidate lives inside on_vkMapMemory, after the pointer is handed to the caller's
     variable and before the function returns, i.e. before the caller can read;
  2. default OFF with its own switch, independent of the flush switch;
  3. `dc civac` only (a plain `dc ivac` would drop the application's dirty lines);
  4. bounded to the allocation being mapped, with a per-mapping cap and a skipped counter;
  5. exactly one civac site and one additional dsb ish;
  6. counters under GFXSTREAM_GUEST_DIAG;
  7. the flush patch's unmap-time clean is still present exactly once: both directions coexist;
  8. the inserted block is brace-balanced.
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
source = (root / 'src/gfxstream/guest/vulkan_enc/ResourceTracker.cpp').read_text(
    encoding='utf-8', errors='replace').replace('\r\n', '\n')

MAP = 'VkResult ResourceTracker::on_vkMapMemory('
UNMAP = 'void ResourceTracker::on_vkUnmapMemory('
MARKER = 'HOSTVISIBLE-INVALIDATE'
CIVAC = '__asm__ __volatile__("dc civac, %0" : : "r"(line) : "memory");'

map_body = source[source.index(MAP):source.index(UNMAP)]
assert map_body.count(MARKER) == 2, 'invalidate must appear once (comment + log tag)'

# 1. placement.
assign = map_body.index('*ppData = deviceMemoryInfo.ptr + offset;')
marker = map_body.index(MARKER)
ret = map_body.index('\n    return host_result;\n}')
assert assign < marker < ret, 'invalidate must run after the pointer is assigned, before return'

block = map_body[marker - 12:ret]

# 2. default off, own switch, checked before the walk.
assert "value && value[0] == '1'" in block, "opt-in must be exactly `value[0] == '1'`"
assert 'GFXSTREAM_HOSTVISIBLE_INVALIDATE' in block
assert block.index("value[0] == '1'") < block.index(CIVAC)


def enabled(value):
    return bool(value) and value[0] == '1'


for value, expected in ((None, False), ('', False), ('0', False), ('1', True), ('1x', True),
                        ('true', False)):
    assert enabled(value) is expected, f'opt-in truth table wrong for {value!r}'

# 3/4. civac only, bounded, capped.
assert block.count(CIVAC) == 1
assert '__asm__ __volatile__("dc ivac' not in block, 'ivac would drop dirty guest lines'
assert 'const uint64_t invalidateSize = deviceMemoryInfo.allocationSize;' in block
assert 'invalidateSize > hostVisibleInvalidateMaxBytes' in block
assert block.index('invalidateSize > hostVisibleInvalidateMaxBytes') < block.index(CIVAC)
assert 'GFXSTREAM_HOSTVISIBLE_FLUSH_MAX_MB' in block
assert '++sInvalidateOverCap;' in block

# 5. the fence.
assert block.count('__asm__ __volatile__("dsb ish" : : : "memory");') == 1
assert source.count('__asm__ __volatile__("dsb ish" : : : "memory");') == 2, \
    'both directions must fence'

# 6. counters.
for counter in ('sInvalidates', 'sInvalidatedBytes', 'sInvalidateOverCap'):
    assert counter in block, f'missing counter {counter}'
assert 'invalidates=%llu bytes=%llu' in block
assert 'guestDiagEnabled()' in block

# 7. both directions coexist, one site each.
assert source.count(CIVAC) == 1
assert source.count('__asm__ __volatile__("dc cvac, %0" : : "r"(line) : "memory");') == 1, \
    'the unmap-time clean must still be present'
assert source.count('GFXSTREAM_HOSTVISIBLE_INVALIDATE') == 1

# 8. balance of the inserted block only.
assert block.count('{') == block.count('}'), 'invalidate block is unbalanced'

report = (
    'PASS invalidate inside on_vkMapMemory, after *ppData, before the return\n'
    'PASS default OFF with its own GFXSTREAM_HOSTVISIBLE_INVALIDATE switch; truth table modelled\n'
    'PASS dc civac only (no dc ivac), bounded to the mapped allocation, cap + over-cap counter\n'
    'PASS one civac site plus the unmap-time cvac site; two dsb ish fences\n'
    'PASS counters invalidates/bytes/over-cap under GFXSTREAM_GUEST_DIAG\n'
    'PASS inserted block brace-balanced\n'
)
print(report, end='')
if out:
    out.mkdir(parents=True, exist_ok=True)
    (out / 'hostvisible-invalidate-source-checks.txt').write_text(report)
