/*
 * Host-compiled unit test for the patched GuestCacheMaintenance.h.
 *
 * The mesa tree is cross-compiled for aarch64, so this file is built on the CI
 * runner with the host g++ against the *generated* header. It exercises the two
 * pieces of the fix that are pure logic and can be wrong in ways the device
 * would only show as a stale screenshot:
 *
 *   - the environment switch (default off must mean off),
 *   - the range clamp (VK_WHOLE_SIZE, zero size, offset at/past the end, and
 *     offset + size overflowing).
 *
 * The cache instructions themselves are aarch64-only and are no-ops here.
 */
#include "GuestCacheMaintenance.h"

#include <cstdint>
#include <cstdio>

using gfxstream::vk::GuestCacheRange;
using gfxstream::vk::kGuestCacheLineSize;
using gfxstream::vk::guestCacheModeFromEnvValue;
using gfxstream::vk::guestCacheRangeForMappedMemory;

namespace {

int failures = 0;

void expect(bool condition, const char* what) {
    if (condition) {
        std::printf("PASS %s\n", what);
    } else {
        std::printf("FAIL %s\n", what);
        ++failures;
    }
}

void expectMode(const char* value, uint32_t expected, const char* what) {
    const uint32_t got = guestCacheModeFromEnvValue(value);
    if (got != expected) {
        std::printf("FAIL mode(%s) = %u, expected %u (%s)\n", value ? value : "(null)", got,
                    expected, what);
        ++failures;
    } else {
        std::printf("PASS mode(%s) = %u (%s)\n", value ? value : "(null)", got, what);
    }
}

void expectRange(uint64_t allocationSize, uint64_t offset, uint64_t size, uint64_t begin,
                 uint64_t end, const char* what) {
    const GuestCacheRange got = guestCacheRangeForMappedMemory(allocationSize, offset, size);
    if (got.begin != begin || got.end != end) {
        std::printf("FAIL range(%llu,%llu,%llu) = {%llu,%llu}, expected {%llu,%llu} (%s)\n",
                    (unsigned long long)allocationSize, (unsigned long long)offset,
                    (unsigned long long)size, (unsigned long long)got.begin,
                    (unsigned long long)got.end, (unsigned long long)begin,
                    (unsigned long long)end, what);
        ++failures;
    } else {
        std::printf("PASS range(%llu,%llu,%llu) = {%llu,%llu} (%s)\n",
                    (unsigned long long)allocationSize, (unsigned long long)offset,
                    (unsigned long long)size, (unsigned long long)got.begin,
                    (unsigned long long)got.end, what);
    }
}

}  // namespace

int main() {
    expect(kGuestCacheLineSize == 64, "cache line size is 64 bytes (16 px * 4 B)");

    // Default off: unset/empty/"0"/"off" select nothing.
    expectMode(nullptr, 0, "unset is off");
    expectMode("", 0, "empty is off");
    expectMode("0", 0, "\"0\" is off");
    expectMode("off", 0, "\"off\" is off");
    // A/B split.
    expectMode("a1", gfxstream::vk::kGuestCacheModeA1, "\"a1\" selects A1 only");
    expectMode("a2", gfxstream::vk::kGuestCacheModeA2, "\"a2\" selects A2 only");
    // Anything else that is not off selects both, so a typo widens the
    // diagnostic instead of silently disabling the fix.
    expectMode("1", 3, "\"1\" selects A1+A2");
    expectMode("on", 3, "\"on\" selects A1+A2");
    expectMode("all", 3, "\"all\" selects A1+A2");
    expectMode("A1", 3, "an unknown value selects A1+A2 (fail open)");
    expectMode("offf", 3, "a near-miss of off selects A1+A2 (fail open)");

    // Range math: clamped to the allocation, rounded out to whole lines.
    expectRange(4096, 0, 0, 0, 0, "zero size maintains nothing");
    expectRange(4096, 0, 1, 0, 64, "one byte covers its line");
    expectRange(4096, 0, UINT64_MAX, 0, 4096, "VK_WHOLE_SIZE covers the allocation");
    expectRange(4096, 4095, UINT64_MAX, 4032, 4096, "VK_WHOLE_SIZE from the last byte");
    expectRange(4096, 4096 - 64, 64, 4032, 4096, "last full line");
    expectRange(0, 0, UINT64_MAX, 0, 0, "zero allocation maintains nothing");
    expectRange(4096, 4096, 1, 0, 0, "offset at the end is empty");
    expectRange(4096, 4097, 1, 0, 0, "offset past the end is empty");
    expectRange(4096, UINT64_MAX, UINT64_MAX, 0, 0, "offset at UINT64_MAX is empty");
    expectRange(4096, 4000, UINT64_MAX - 1, 3968, 4096,
                "offset + size overflow is clamped, not wrapped");
    expectRange(4096, 64, 64, 64, 128, "an aligned mid range is exact");
    expectRange(4096, 64, 1, 64, 128, "an unaligned end rounds up");
    expectRange(4095, 0, UINT64_MAX, 0, 4096,
                "a non-line-aligned allocation rounds up inside its 4 KiB reservation");
    expectRange(64, 0, 64, 0, 64, "a one-line allocation is exact");

    if (failures) {
        std::printf("%d guest cache-maintenance unit test(s) failed\n", failures);
        return 1;
    }
    std::printf("all guest cache-maintenance unit tests passed\n");
    return 0;
}
