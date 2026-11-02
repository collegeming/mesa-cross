#!/usr/bin/env python3
"""Source checks for the guest ring-wakeup fix; source-level only, no runtime proof."""
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1]).resolve()
cpp = (root / 'src/gfxstream/guest/GoldfishAddressSpace/AddressSpaceStream.cpp').read_text()
hdr = (root / 'src/gfxstream/guest/GoldfishAddressSpace/include/AddressSpaceStream.h').read_text()

for text in [
    'const uint32_t outstandingBytes = maxOutstanding * sizeForRing;',
    'while (ringAvailReadNow >= outstandingBytes) {',
    'if (ringAvailReadNow < outstandingBytes) {',
    'outstandingPingedHost',
    'deepWaitWantsPing()',
    'm_reportedType1Stall',
]:
    assert text in cpp, text
assert 'while (ringAvailReadNow >= maxOutstanding * sizeForRing) {' not in cpp
assert 'bool m_reportedType1Stall = false;' in hdr
# The send-loop ping must now also fire from the deep-wait path.
assert ('deepWaitWantsPing()) {\n'
        '            notifyAvailable();\n'
        '            hostPinged = true;') in cpp

changed = set(subprocess.check_output(
    ['git', '-C', str(root), 'diff', '--name-only', 'HEAD', '--'], text=True).splitlines())
ring_files = {
    'src/gfxstream/guest/GoldfishAddressSpace/AddressSpaceStream.cpp',
    'src/gfxstream/guest/GoldfishAddressSpace/include/AddressSpaceStream.h',
}
assert ring_files <= changed, changed
print('ring-wake guest checks: OK')
