#!/usr/bin/env python3
"""Log why an external image skipped the CIB1 classic-image conversion.

A failed conversion sends the create down the plain external-DMA_BUF path, where this Mali
driver rejects the image, so the first non-CIB1 external create after the bridge is negotiated
is the interesting one. Log every condition input (including the values the create-time checks
use but the image-format query cannot see) for the first few occurrences, then stop.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

REL = 'src/gfxstream/guest/vulkan_enc/ResourceTracker.cpp'
path = root / REL
text = path.read_text(encoding='utf-8')

OLD = '''    if (cib1) {
        const uint32_t width = pCreateInfo->extent.width, height = pCreateInfo->extent.height;
'''

NEW = '''    if (!cib1 && extImgCiPtr) {
        static std::atomic<unsigned> cib1ReasonLogs{0};
        if (cib1ReasonLogs.fetch_add(1, std::memory_order_relaxed) < 16) {
            const auto* cib1ViewLog =
                vk_find_struct_const(pCreateInfo, IMAGE_FORMAT_LIST_CREATE_INFO);
            const auto* cib1ModLog =
                vk_find_struct_const(pCreateInfo, IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT);
            mesa_loge("CIB1-REASON: ack=%d handles=0x%x linear=%d views=%d sharing=%d "
                      "initial-layout=%d explicit-mod=%d geom=%d type=%d fmt=%d flags=0x%x "
                      "usage=0x%x depth=%u mips=%u layers=%u samples=0x%x mods=%u",
                      (int)mCaps.classicImageBridgeAck, (unsigned)extImgCiPtr->handleTypes,
                      (int)cib1Linear,
                      (int)cib1Views(pCreateInfo->format, pCreateInfo->flags, cib1ViewLog),
                      (int)pCreateInfo->sharingMode, (int)pCreateInfo->initialLayout,
                      (int)(vk_find_struct_const(pCreateInfo,
                             IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT) != nullptr),
                      (int)cib1Geometry(pCreateInfo->imageType, pCreateInfo->format,
                          pCreateInfo->flags, pCreateInfo->usage, pCreateInfo->extent.depth,
                          pCreateInfo->mipLevels, pCreateInfo->arrayLayers, pCreateInfo->samples),
                      (int)pCreateInfo->imageType, (int)pCreateInfo->format,
                      (unsigned)pCreateInfo->flags, (unsigned)pCreateInfo->usage,
                      pCreateInfo->extent.depth, pCreateInfo->mipLevels,
                      pCreateInfo->arrayLayers, (unsigned)pCreateInfo->samples,
                      cib1ModLog ? cib1ModLog->drmFormatModifierCount : 0);
        }
    }
    if (cib1) {
        const uint32_t width = pCreateInfo->extent.width, height = pCreateInfo->extent.height;
'''

if text.count(OLD) != 1:
    raise SystemExit(f'{REL}: expected one CIB1 create anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW, 1)

if '#include <atomic>' not in text:
    anchor = '#include <algorithm>'
    if text.count(anchor) != 1:
        raise SystemExit(f'{REL}: expected one <algorithm> include, found {text.count(anchor)}')
    text = text.replace(anchor, '#include <algorithm>\n#include <atomic>', 1)

path.write_text(text, encoding='utf-8', newline='')
print('added CIB1 skip-reason diagnostics (first 16 external creates)')
