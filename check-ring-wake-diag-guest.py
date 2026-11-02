#!/usr/bin/env python3
"""Source checks for the type1 wait diagnostics."""
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
cpp = (root / 'src/gfxstream/guest/GoldfishAddressSpace/AddressSpaceStream.cpp').read_text()
hdr = (root / 'src/gfxstream/guest/GoldfishAddressSpace/include/AddressSpaceStream.h').read_text()

for text in [
    'gfxstream: type1 outstanding wait: ring=%p write=%u read=%u avail=%u ',
    'gfxstream: type1 send wait: ring=%p write=%u read=%u avail=%u want=%zu ',
    'bool m_reportedType1SendStall = false;',
]:
    assert text in cpp or text in hdr, text
assert 'bool m_reportedType1SendStall = false;' in hdr
print('ring-wake diag guest checks: OK')
