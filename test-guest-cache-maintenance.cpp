/*
 * Host-compiled unit test for the patched GuestCacheMaintenance.h.
 *
 * The mesa tree is cross-compiled for aarch64, so this file is built on the CI
 * runner with the host g++ against the *generated* header. It exercises the
 * pieces of the fix that are pure logic and can be wrong in ways the device
 * would only show as a stale screenshot or a dead compositor:
 *
 *   - the default: unset/empty enables every mechanism, "0"/"off" disables it;
 *   - the per-mechanism split and the comma parser;
 *   - the range clamp (VK_WHOLE_SIZE, zero size, offset at/past the end, and
 *     offset + size overflowing);
 *   - the statistics line format and the fact that counters stay untouched
 *     unless the stats switch is on (the source-level half of that assertion
 *     lives in check-guest-readback-invalidate.py);
 *   - the one-time parse: the mode is not re-read after the first call.
 *
 * The cache instructions themselves are aarch64-only and are no-ops here, so
 * the UCI probe reports "not applicable" (-1) on the CI runner.
 */
#include "GuestCacheMaintenance.h"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include <unistd.h>

using gfxstream::vk::GuestCacheConfig;
using gfxstream::vk::GuestCacheRange;
using gfxstream::vk::guestCacheConfigFromEnvValues;
using gfxstream::vk::guestCacheCounters;
using gfxstream::vk::guestCacheMaintenanceMode;
using gfxstream::vk::guestCacheModeFromEnvValue;
using gfxstream::vk::guestCacheProbeEl0Maintenance;
using gfxstream::vk::guestCacheRangeForMappedMemory;
using gfxstream::vk::guestCacheStatsEnabled;
using gfxstream::vk::guestCacheStatsPrint;
using gfxstream::vk::guestCacheStatsRecord;
using gfxstream::vk::kGuestCacheLineSize;
using gfxstream::vk::kGuestCacheModeA1;
using gfxstream::vk::kGuestCacheModeA2;
using gfxstream::vk::kGuestCacheModeAll;
using gfxstream::vk::kGuestCacheModeCMap;
using gfxstream::vk::kGuestCacheModeCUnmap;
using gfxstream::vk::kGuestCacheModeStats;
using gfxstream::vk::kGuestCacheStatMap;
using gfxstream::vk::kGuestCacheStatUnmap;
using gfxstream::vk::kGuestCacheStatWait;

// Compile-time assertions: the mode bits, the default, and the range math are
// all constant expressions, so a regression fails the build, not a device run.
static_assert(kGuestCacheModeAll == 15, "all four mechanisms use bits 0..3");
static_assert(kGuestCacheModeA1 == 1 && kGuestCacheModeA2 == 2 &&
                  kGuestCacheModeCMap == 4 && kGuestCacheModeCUnmap == 8,
              "each mechanism keeps its own bit for device-side splitting");
static_assert((kGuestCacheModeAll & kGuestCacheModeStats) == 0,
              "the statistics flag is not a maintenance mode");
static_assert(kGuestCacheLineSize == 64, "16 px * 4 B is one cache line");
static_assert(guestCacheModeFromEnvValue(nullptr) == kGuestCacheModeAll,
              "unset means everything on");
static_assert(guestCacheModeFromEnvValue("") == kGuestCacheModeAll,
              "set but empty means everything on");
static_assert(guestCacheModeFromEnvValue("0") == 0, "\"0\" disables everything");
static_assert(guestCacheModeFromEnvValue("off") == 0, "\"off\" disables everything");
static_assert(guestCacheModeFromEnvValue("a1,cmap") == (kGuestCacheModeA1 | kGuestCacheModeCMap),
              "a comma list selects only its modes");
static_assert(guestCacheRangeForMappedMemory(4096, 0, UINT64_MAX).end == 4096,
              "VK_WHOLE_SIZE is clamped to the allocation");
static_assert(guestCacheRangeForMappedMemory(4096, 4000, UINT64_MAX - 1).begin == 3968,
              "offset + size overflow must not wrap");

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

// Captures the stats line written to fd 2. guestCacheStatsPrint() uses write(2)
// so that the SIGUSR1 handler may call it; the test checks the exact bytes.
std::string captureStatsLine() {
    FILE* capture = std::tmpfile();
    if (!capture) return std::string();
    std::fflush(stderr);
    const int savedFd = dup(STDERR_FILENO);
    if (savedFd < 0) {
        std::fclose(capture);
        return std::string();
    }
    dup2(fileno(capture), STDERR_FILENO);
    guestCacheStatsPrint();
    dup2(savedFd, STDERR_FILENO);
    close(savedFd);
    std::rewind(capture);
    char line[256] = {};
    const size_t count = std::fread(line, 1, sizeof(line) - 1, capture);
    std::fclose(capture);
    return std::string(line, count);
}

}  // namespace

int main() {
    // The default must not depend on the test runner's environment.
    unsetenv("GFXSTREAM_GUEST_INVALIDATE");
    unsetenv("GFXSTREAM_GUEST_INVALIDATE_STATS");
    unsetenv("GFXSTREAM_GUEST_INVALIDATE_DEBUG");

    expect(kGuestCacheLineSize == 64, "cache line size is 64 bytes (16 px * 4 B)");

    // Default on: unset/empty select everything, "0"/"off" select nothing.
    expectMode(nullptr, kGuestCacheModeAll, "unset enables everything");
    expectMode("", kGuestCacheModeAll, "empty enables everything");
    expectMode("0", 0, "\"0\" restores the baseline");
    expectMode("off", 0, "\"off\" restores the baseline");
    // Each mechanism has its own bit, so a device run can split them.
    expectMode("a1", kGuestCacheModeA1, "\"a1\" selects A1 only");
    expectMode("a2", kGuestCacheModeA2, "\"a2\" selects A2 only");
    expectMode("cmap", kGuestCacheModeCMap, "\"cmap\" selects map-invalidate only");
    expectMode("cunmap", kGuestCacheModeCUnmap, "\"cunmap\" selects unmap-clean only");
    expectMode("cmap,cunmap", kGuestCacheModeCMap | kGuestCacheModeCUnmap,
               "\"cmap,cunmap\" selects both mapping directions");
    expectMode("cunmap,cmap", kGuestCacheModeCMap | kGuestCacheModeCUnmap,
               "comma order does not matter");
    expectMode("a1,cmap", kGuestCacheModeA1 | kGuestCacheModeCMap,
               "a comma list mixes API and mapping modes");
    expectMode("a1,a2,cmap,cunmap", kGuestCacheModeAll, "all four named modes");
    expectMode("cmap,", kGuestCacheModeCMap, "a trailing comma is harmless");
    expectMode(",cunmap", kGuestCacheModeCUnmap, "a leading comma is harmless");
    // Anything else that is not off selects everything, so a typo widens the
    // diagnostic instead of silently disabling the fix.
    expectMode("1", kGuestCacheModeAll, "\"1\" selects everything");
    expectMode("on", kGuestCacheModeAll, "\"on\" selects everything");
    expectMode("all", kGuestCacheModeAll, "\"all\" selects everything");
    expectMode("A1", kGuestCacheModeAll, "an unknown value selects everything (fail open)");
    expectMode("offf", kGuestCacheModeAll, "a near-miss of off selects everything (fail open)");
    expectMode("bogus", kGuestCacheModeAll, "an unknown token selects everything (fail open)");
    expectMode("0,cmap", kGuestCacheModeAll,
               "a stray off token cannot silently narrow an explicit list");

    // The whole configuration word: mode bits plus the independent diagnostics.
    {
        const GuestCacheConfig defaults = guestCacheConfigFromEnvValues(nullptr, nullptr, nullptr);
        expect(defaults.mode == kGuestCacheModeAll && !defaults.stats && !defaults.debug,
               "unset mode/stats/debug resolves to maintenance on, no diagnostics");
        const GuestCacheConfig split =
            guestCacheConfigFromEnvValues("cmap,cunmap", "1", "true");
        expect(split.mode == (kGuestCacheModeCMap | kGuestCacheModeCUnmap | kGuestCacheModeStats),
               "stats joins the same word without changing the mode bits");
        expect(split.stats && split.debug, "\"1\"/\"true\" enable both diagnostics");
        const GuestCacheConfig disabled = guestCacheConfigFromEnvValues("0", "0", "off");
        expect(disabled.mode == 0 && !disabled.stats && !disabled.debug,
               "\"0\" disables maintenance and statistics together");
    }

    // Range math: clamped to the allocation, rounded out to whole lines.
    expectRange(4096, 0, 0, 0, 0, "zero size maintains nothing");
    expectRange(4096, 0, 1, 0, 64, "one byte covers its line");
    expectRange(4096, 0, UINT64_MAX, 0, 4096, "VK_WHOLE_SIZE covers the allocation");
    expectRange(4096, 4095, UINT64_MAX, 4032, 4096, "VK_WHOLE_SIZE from the last byte");
    expectRange(4096, 4096 - 64, 64, 4032, 4096, "last full line");
    expectRange(0, 0, UINT64_MAX, 0, 0, "zero allocation maintains nothing");
    expectRange(0, 0, 4096, 0, 0, "an unresolved limit maintains nothing");
    expectRange(8192, 4096, 4096, 4096, 8192, "a map at an offset is bounded by the allocation");
    expectRange(8192, 4096, UINT64_MAX, 4096, 8192, "VK_WHOLE_SIZE from a map offset");
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

    // The mode is resolved once: a later setenv must not change it, which is
    // what makes the pthread-once parse (and the UCI probe) safe to rely on.
    const uint32_t initialMode = guestCacheMaintenanceMode();
    expect(initialMode == kGuestCacheModeAll,
           "first resolution reads the default from the environment");
    expect(!guestCacheStatsEnabled(), "statistics are off unless STATS=1");
    setenv("GFXSTREAM_GUEST_INVALIDATE", "0", 1);
    expect(guestCacheMaintenanceMode() == initialMode,
           "the mode is parsed once, not on every entry point");
    unsetenv("GFXSTREAM_GUEST_INVALIDATE");
    if (guestCacheProbeEl0Maintenance() != -1) {
        std::printf("NOTE the UCI probe is compiled in on this host; CI is x86 so it is not\n");
    }

    // Statistics: exactly one fixed-format line, and nothing is recorded unless
    // guestCacheStatsRecord() is called (the callers guard it with the mode).
    expect(guestCacheCounters().maps.load() == 0 && guestCacheCounters().bytes.load() == 0,
           "counters stay at zero while statistics are disabled");
    guestCacheStatsRecord(kGuestCacheStatMap, 64, 7);
    guestCacheStatsRecord(kGuestCacheStatUnmap, 32, 3);
    guestCacheStatsRecord(kGuestCacheStatWait, 128, 5);
    const std::string statsLine = captureStatsLine();
    expect(statsLine == "GUEST-CMO-STATS maps=1 unmap=1 waits=1 bytes=224 ns=15\n",
           "the statistics line has the fixed prefix, field order and sums");

    if (failures) {
        std::printf("%d guest cache-maintenance unit test(s) failed\n", failures);
        return 1;
    }
    std::printf("all guest cache-maintenance unit tests passed\n");
    return 0;
}
