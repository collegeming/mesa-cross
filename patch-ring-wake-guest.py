#!/usr/bin/env python3
"""Let the guest's type1 write wait ask the host for a wakeup instead of spinning forever.

type1Write has two waits. The one in the send loop already pinged a parked host; the one that
waits for queued transfers to drain did not, so a consumer that parked after missing a write
left a vcpu at ~100% with nothing on the host running. This adds the same rate-limited ping
(the existing deepWaitWantsPing()/backoff() machinery) to that wait and keeps the ring-space
wait pinging while it is deep.
"""
import argparse
import difflib
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

CPP = 'src/gfxstream/guest/GoldfishAddressSpace/AddressSpaceStream.cpp'
HDR = 'src/gfxstream/guest/GoldfishAddressSpace/include/AddressSpaceStream.h'

OLD_WAIT = """    uint32_t ringAvailReadNow = ring_buffer_available_read(m_context.to_host, 0);

    while (ringAvailReadNow >= maxOutstanding * sizeForRing) {
        ringAvailReadNow = ring_buffer_available_read(m_context.to_host, 0);
    }
"""

NEW_WAIT = """    // Waiting for queued transfers to drain, and the one wait in this file that could never ask
    // the host for anything: a bare spin on a counter the host only moves by consuming. If the
    // consumer parked on top of work it had not seen, this loop never ended -- one vcpu at ~100%
    // with every host render thread asleep. The host now re-reads the ring after arming its park
    // state, which closes that ordering window; the ping here keeps a lost wakeup recoverable
    // instead of permanent.
    const uint32_t outstandingBytes = maxOutstanding * sizeForRing;
    uint32_t ringAvailReadNow = ring_buffer_available_read(m_context.to_host, 0);
    bool outstandingPingedHost = false;

    while (ringAvailReadNow >= outstandingBytes) {
        ring_buffer_yield();
        backoff();
        ringAvailReadNow = ring_buffer_available_read(m_context.to_host, 0);
        if (ringAvailReadNow < outstandingBytes) {
            break;
        }
        if ((!outstandingPingedHost &&
             *(m_context.host_state) != ASG_HOST_STATE_CAN_CONSUME &&
             *(m_context.host_state) != ASG_HOST_STATE_RENDERING) ||
            deepWaitWantsPing()) {
            if (!m_reportedType1Stall) {
                m_reportedType1Stall = true;
                mesa_logw(
                    "gfxstream: type1 write stalled on unread transfers: %u bytes queued, "
                    "limit %u, host_state=%u",
                    ringAvailReadNow, outstandingBytes, *(m_context.host_state));
            }
            notifyAvailable();
            outstandingPingedHost = true;
        }
        if (isInError()) {
            return -1;
        }
    }
"""

OLD_PING = """        if (!hostPinged &&
            *(m_context.host_state) != ASG_HOST_STATE_CAN_CONSUME &&
            *(m_context.host_state) != ASG_HOST_STATE_RENDERING) {
            notifyAvailable();
            hostPinged = true;
        }
"""

NEW_PING = """        if ((!hostPinged &&
             *(m_context.host_state) != ASG_HOST_STATE_CAN_CONSUME &&
             *(m_context.host_state) != ASG_HOST_STATE_RENDERING) ||
            deepWaitWantsPing()) {
            notifyAvailable();
            hostPinged = true;
        }
"""

OLD_MEMBER = """    bool m_reportedDeepWait = false;
"""

NEW_MEMBER = """    bool m_reportedDeepWait = false;
    // One warning per stream for the transfer-limit wait in type1Write, same purpose as the
    // deep-wait report above.
    bool m_reportedType1Stall = false;
"""


def edit(rel, old, new):
    path = root / rel
    text = path.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise SystemExit(f'{rel}: expected one occurrence, found {text.count(old)}')
    path.write_text(text.replace(old, new), encoding='utf-8', newline='')
    return text, text.replace(old, new)


for rel, old, new in ((CPP, OLD_WAIT, NEW_WAIT), (CPP, OLD_PING, NEW_PING), (HDR, OLD_MEMBER, NEW_MEMBER)):
    before, after = edit(rel, old, new)
    print('patched', rel)
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), rel, rel, n=2):
        print(line)
