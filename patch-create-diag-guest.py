#!/usr/bin/env python3
"""Log every guest vkCreateImage (bounded) to find the screenshot image shape.

The CIB1 diagnostics only describe images that reach the bridge condition; the
compositor's screenshot image creates no CIB1 and no host-side external image
trace, so its exact VkImageCreateInfo is unknown. Log the first N creates with
format/type/extent/tiling/usage/flags/external/sharing/initialLayout so the
screenshot image can be compared against the bridge contract.
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

    VkImageCreateInfo localCreateInfo = vk_make_orphan_copy(*pCreateInfo);'''
NEW = '''    VkEncoder* enc = (VkEncoder*)context;

#if defined(LINUX_GUEST_BUILD)
    {
        // Bounded diagnostic: the first creates describe the compositor's render
        // targets and screenshot image; compare their shape with the CIB1 contract.
        static std::atomic<uint32_t> sImgCreateLogs{0};
        if (sImgCreateLogs.fetch_add(1, std::memory_order_relaxed) < 64) {
            const auto* diagExt =
                vk_find_struct_const(pCreateInfo, EXTERNAL_MEMORY_IMAGE_CREATE_INFO);
            const auto* diagExplicit = vk_find_struct_const(
                pCreateInfo, IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT);
            const auto* diagModList = vk_find_struct_const(
                pCreateInfo, IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT);
            const auto* diagViews =
                vk_find_struct_const(pCreateInfo, IMAGE_FORMAT_LIST_CREATE_INFO);
            const uint64_t diagMod0 =
                (diagModList && diagModList->drmFormatModifierCount > 0 &&
                 diagModList->pDrmFormatModifiers)
                    ? diagModList->pDrmFormatModifiers[0]
                    : ~0ull;
            mesa_logw(
                "IMG-CREATE: fmt=%u type=%u extent=%ux%u tiling=%u usage=0x%x flags=0x%x "
                "ext=0x%x sharing=%u initial=%u explicit=%d modlist=%d mod0=0x%llx "
                "views=%u view0=%u view1=%u",
                pCreateInfo->format, pCreateInfo->imageType, pCreateInfo->extent.width,
                pCreateInfo->extent.height, pCreateInfo->tiling, pCreateInfo->usage,
                pCreateInfo->flags, diagExt ? diagExt->handleTypes : 0,
                pCreateInfo->sharingMode, pCreateInfo->initialLayout, diagExplicit ? 1 : 0,
                diagModList ? 1 : 0, (unsigned long long)diagMod0,
                diagViews ? diagViews->viewFormatCount : 0,
                (diagViews && diagViews->viewFormatCount > 0) ? diagViews->pViewFormats[0] : 0,
                (diagViews && diagViews->viewFormatCount > 1) ? diagViews->pViewFormats[1] : 0);
        }
    }
#endif

    VkImageCreateInfo localCreateInfo = vk_make_orphan_copy(*pCreateInfo);'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one create-image anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
rt.write_text(text, encoding='utf-8', newline='')
print('guest create-image diagnostic patched')
