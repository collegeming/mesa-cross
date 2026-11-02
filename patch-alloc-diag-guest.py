#!/usr/bin/env python3
"""Log every guest vkAllocateMemory (bounded) to see the image-memory path.

The host shows CIB1 aliases created for the 1280x720 output but no guest
allocation ever arrives. Log size/memoryType/import-fd/import-colorbuffer and
the dedicated image/buffer for the first N allocations so the exact allocation
shape (or its absence) for the output image is visible.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

rt = root / 'src/gfxstream/guest/vulkan_enc/ResourceTracker.cpp'
text = rt.read_text(encoding='utf-8')

if '#include <atomic>' not in text:
    anchor = '#include <algorithm>'
    if text.count(anchor) != 1:
        raise SystemExit(f'expected one algorithm include, found {text.count(anchor)}')
    text = text.replace(anchor, anchor + '\n#include <atomic>', 1)

OLD = '''    VkEncoder* enc = (VkEncoder*)context;

    bool hasDedicatedImage = false;'''
NEW = '''    VkEncoder* enc = (VkEncoder*)context;

#if defined(LINUX_GUEST_BUILD)
    {
        // Bounded diagnostic: which allocation shape (fd import, ColorBuffer
        // import, host blob) is requested for image memory, and whether the
        // compositor's output size appears at all.
        static std::atomic<uint32_t> sAllocDiagLogs{0};
        if (sAllocDiagLogs.fetch_add(1, std::memory_order_relaxed) < 256) {
            const auto* diagFd =
                vk_find_struct_const(pAllocateInfo, IMPORT_MEMORY_FD_INFO_KHR);
            const auto* diagCb =
                vk_find_struct_const(pAllocateInfo, IMPORT_COLOR_BUFFER_GOOGLE);
            const auto* diagDed =
                vk_find_struct_const(pAllocateInfo, MEMORY_DEDICATED_ALLOCATE_INFO);
            mesa_logw("ALLOC-DIAG: size=%llu type=%u fd=%d cb=%u dedImg=%p dedBuf=%p",
                      (unsigned long long)pAllocateInfo->allocationSize,
                      pAllocateInfo->memoryTypeIndex, diagFd ? diagFd->fd : -1,
                      diagCb ? diagCb->colorBuffer : 0,
                      diagDed ? (void*)diagDed->image : nullptr,
                      diagDed ? (void*)diagDed->buffer : nullptr);
        }
    }
#endif

    bool hasDedicatedImage = false;'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one allocate anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
rt.write_text(text, encoding='utf-8', newline='')
print('guest allocate diagnostic patched')
