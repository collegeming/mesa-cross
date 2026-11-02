#!/usr/bin/env python3
"""Log the exact ring identity and positions at both type1Write waits.

The stall report landed in neither wait's log last time, so the wait and the ring it was
looking at could not be told apart. One line per stream, at the first occurrence of each
wait, with the ring pointer and both raw positions.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

CPP = 'src/gfxstream/guest/GoldfishAddressSpace/AddressSpaceStream.cpp'
HDR = 'src/gfxstream/guest/GoldfishAddressSpace/include/AddressSpaceStream.h'

OLD_STALL = """            if (!m_reportedType1Stall) {
                m_reportedType1Stall = true;
                mesa_logw(
                    "gfxstream: type1 write stalled on unread transfers: %u bytes queued, "
                    "limit %u, host_state=%u",
                    ringAvailReadNow, outstandingBytes, *(m_context.host_state));
            }
"""

NEW_STALL = """            if (!m_reportedType1Stall) {
                m_reportedType1Stall = true;
                const uint32_t diagWrite =
                    __atomic_load_n(&m_context.to_host->write_pos, __ATOMIC_ACQUIRE);
                const uint32_t diagRead =
                    __atomic_load_n(&m_context.to_host->read_pos, __ATOMIC_ACQUIRE);
                mesa_logw(
                    "gfxstream: type1 outstanding wait: ring=%p write=%u read=%u avail=%u "
                    "limit=%u host_state=%u",
                    (void*)m_context.to_host, diagWrite, diagRead, ringAvailReadNow,
                    outstandingBytes, *(m_context.host_state));
            }
"""

OLD_SEND = """        if (sentChunks == 0) {
            ring_buffer_yield();
            backoff();
        }

        sent += sentChunks * (sizeForRing - sent);
"""

NEW_SEND = """        if (sentChunks == 0) {
            if (!m_reportedType1SendStall) {
                m_reportedType1SendStall = true;
                const uint32_t diagWrite =
                    __atomic_load_n(&m_context.to_host->write_pos, __ATOMIC_ACQUIRE);
                const uint32_t diagRead =
                    __atomic_load_n(&m_context.to_host->read_pos, __ATOMIC_ACQUIRE);
                mesa_logw(
                    "gfxstream: type1 send wait: ring=%p write=%u read=%u avail=%u want=%zu "
                    "sent=%zu host_state=%u",
                    (void*)m_context.to_host, diagWrite, diagRead,
                    ring_buffer_available_read(m_context.to_host, 0), sizeForRing, sent,
                    *(m_context.host_state));
            }
            ring_buffer_yield();
            backoff();
        }

        sent += sentChunks * (sizeForRing - sent);
"""

OLD_MEMBER = """    bool m_reportedType1Stall = false;
"""

NEW_MEMBER = """    bool m_reportedType1Stall = false;
    bool m_reportedType1SendStall = false;
"""


def edit(rel, old, new):
    path = root / rel
    text = path.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise SystemExit(f'{rel}: expected one occurrence, found {text.count(old)}')
    path.write_text(text.replace(old, new), encoding='utf-8', newline='')
    print('patched', rel)


edit(CPP, OLD_STALL, NEW_STALL)
edit(CPP, OLD_SEND, NEW_SEND)
edit(HDR, OLD_MEMBER, NEW_MEMBER)
