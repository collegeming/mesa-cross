#!/usr/bin/env python3
"""CIB1 export-only guest patch, applied AFTER exact cecc0e96 modifier fix in Actions.
No local compilation/execution authorization. Native requirements come from paired host CB.
The dedicated allocation branch takes the ColorBuffer import even when the app's allocation
names no export/import handle: the bridge owns the backing, so that metadata cannot decide
whether the image binds. The image owns the GEM staging resource, allocation is claimed and
released exactly once through classicBridgeMemory, and the resource is retired until process
exit instead of being closed while the host may still drain work into its pages.
"""
import argparse
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
a = p.parse_args()
root = a.root.resolve()

def edit(rel, old, new):
    path = root / rel
    text = path.read_text()
    if text.count(old) != 1:
        raise SystemExit(f'{rel}: expected unique anchor: {old[:100]}')
    path.write_text(text.replace(old, new))

protocol = 'src/virtio/virtio-gpu/virtgpu_gfxstream_protocol.h'
rt = 'src/gfxstream/guest/vulkan_enc/ResourceTracker.cpp'
rh = 'src/gfxstream/guest/vulkan_enc/ResourceTracker.h'
virt = 'src/gfxstream/guest/platform/include/VirtGpu.h'
drm = 'src/gfxstream/guest/platform/drm/DrmVirtGpuDevice.cpp'
edit(protocol, '    uint32_t guestAllocSizeMb;\n};', '''    uint32_t guestAllocSizeMb;
    // CIB1 append-only. Old hosts copy only 120 bytes into a zero-initialized tail.
    uint32_t classicImageBridgeVersion;
    uint32_t classicImageBridgeContract;
};
#ifdef __cplusplus
static_assert(sizeof(vulkanCapset) == 128, "CIB1 capset size");
static_assert(__builtin_offsetof(vulkanCapset, classicImageBridgeVersion) == 120, "CIB1 version offset");
static_assert(__builtin_offsetof(vulkanCapset, classicImageBridgeContract) == 124, "CIB1 contract offset");
static inline bool cib1CapAcknowledged(int result, const vulkanCapset& caps) {
    return result == 0 && caps.classicImageBridgeVersion == 1 &&
        caps.classicImageBridgeContract == 0x43494231u;
}
#endif''')
edit(virt, 'struct VirtGpuCaps {\n', 'struct VirtGpuCaps {\n    bool classicImageBridgeAck = false;\n')
# GET_CAPS has no returned-size userspace field. Kernel copies min(request,host
# advertised capset max_size); successful exact nonzero tail witnesses >=128.
edit(drm, '    // DroidVM: report the pVM guest-alloc pool partition the host announced via capset.', '''    if (capset == kCapsetGfxStreamVulkan && cib1CapAcknowledged(ret, mCaps.vulkanCapset)) {
        mCaps.classicImageBridgeAck = true;
    } else {
        mCaps.vulkanCapset.classicImageBridgeVersion = 0;
        mCaps.vulkanCapset.classicImageBridgeContract = 0;
    }
    // DroidVM: report the pVM guest-alloc pool partition the host announced via capset.''')
edit(rt, '#include "vulkan/vulkan_core.h"', '#include "vulkan/vulkan_core.h"\n#if defined(LINUX_GUEST_BUILD)\n#include "vk_android_native_buffer_gfxstream.h"\n#include <limits>\n#include <atomic>\n#endif')
edit(rh, '        VirtGpuResourcePtr blobPtr = nullptr;\n', '''        VirtGpuResourcePtr blobPtr = nullptr;
        VkImage classicBridgeImage = VK_NULL_HANDLE;
        bool classicBridgeBound = false;
''')
edit(rh, '        VkDeviceMemory currentBacking = VK_NULL_HANDLE;\n        VkDeviceSize currentBackingOffset = 0;\n        VkDeviceSize currentBackingSize = 0;\n        bool baseRequirementsKnown = false;\n        VkMemoryRequirements baseRequirements;\n#ifdef VK_USE_PLATFORM_ANDROID_KHR', '''        VkDeviceMemory currentBacking = VK_NULL_HANDLE;
        VkDeviceSize currentBackingOffset = 0;
        VkDeviceSize currentBackingSize = 0;
        bool baseRequirementsKnown = false;
        VkMemoryRequirements baseRequirements;
        VirtGpuResourcePtr classicBridgeResource = nullptr;
        bool classicBridgeAllocated = false;
        VkDeviceMemory classicBridgeMemory = VK_NULL_HANDLE;
#ifdef VK_USE_PLATFORM_ANDROID_KHR''')
# Base images are UNORM; bounded mutable views stay inside the matching channel family.
helpers = '''#if defined(LINUX_GUEST_BUILD)
static bool cib1Geometry(VkImageType type, VkFormat format, VkImageCreateFlags flags,
                         VkImageUsageFlags usage, uint32_t depth, uint32_t mips,
                         uint32_t layers, VkSampleCountFlagBits samples) {
    const VkImageUsageFlags allowed = VK_IMAGE_USAGE_TRANSFER_SRC_BIT |
        VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
        VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_INPUT_ATTACHMENT_BIT;
    return type == VK_IMAGE_TYPE_2D &&
        (format == VK_FORMAT_R8G8B8A8_UNORM || format == VK_FORMAT_B8G8R8A8_UNORM) &&
        !(flags & ~(VK_IMAGE_CREATE_ALIAS_BIT | VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT)) &&
        usage && !(usage & ~allowed) &&
        depth == 1 && mips == 1 && layers == 1 && samples == VK_SAMPLE_COUNT_1_BIT;
}
static bool cib1Views(VkFormat base, VkImageCreateFlags flags,
                     const VkImageFormatListCreateInfo* list) {
    if (!list) return true;
    if (!list->viewFormatCount || list->viewFormatCount > 2 || !list->pViewFormats)
        return false;
    const VkFormat srgb = base == VK_FORMAT_R8G8B8A8_UNORM
        ? VK_FORMAT_R8G8B8A8_SRGB : VK_FORMAT_B8G8R8A8_SRGB;
    for (uint32_t i = 0; i < list->viewFormatCount; ++i) {
        if (list->pViewFormats[i] != base && list->pViewFormats[i] != srgb) return false;
        if (i && list->pViewFormats[i] == list->pViewFormats[0]) return false;
        if (!(flags & VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT) && list->pViewFormats[i] != base)
            return false;
    }
    return true;
}
static bool cib1Layout(uint32_t width, uint32_t height, uint32_t* stride, uint32_t* size) {
    const uint64_t row = (uint64_t(width) * 4 + 15) & ~uint64_t(15);
    if (!width || !height || row > UINT32_MAX || height > UINT32_MAX / row ||
        row / 4 > static_cast<uint64_t>(std::numeric_limits<int>::max())) return false;
    *stride = static_cast<uint32_t>(row);
    *size = static_cast<uint32_t>(row * height);
    return true;
}
static bool cib1BindIdentity(bool imageBridge, bool memoryBridge, bool sameImage,
                             bool sameOwner, bool sameMemory, bool alreadyBound,
                             VkDeviceSize offset) {
    return imageBridge && memoryBridge && sameImage && sameOwner && sameMemory &&
        !alreadyBound && !offset;
}
static bool cib1RetainAllocation(bool imageExists, bool sameOwner, bool hostSuccess) {
    return imageExists && sameOwner && hostSuccess;
}
// The image is the single owner of the GEM staging resource; the memory holds a second
// shared reference only so export can reach it. Allocation is claimed exactly once and
// released exactly once, keyed by the image/memory pair.
static bool cib1BridgeClaim(bool imageAlive, bool sameDevice, bool alreadyAllocated,
                            bool dedicatedBuffer, bool sizeMatches, bool indexValid) {
    return imageAlive && sameDevice && !alreadyAllocated && !dedicatedBuffer &&
        sizeMatches && indexValid;
}
static bool cib1BridgeRelease(bool imageAlive, bool sameMemory, bool memoryAlive) {
    return imageAlive && sameMemory && memoryAlive;
}
static bool cib1Handles(VkExternalMemoryHandleTypeFlags handles) {
    const auto allowed = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT |
                         VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
    return handles && !(handles & ~allowed);
}
#endif

#if defined(LINUX_GUEST_BUILD)
// The guest flushes vkFreeMemory/vkDestroyImage and immediately runs the encoder's destroy
// mapping, but the host may still be draining ring commands and GPU work that writes into
// the ColorBuffer backed by this staging resource. Closing the GEM handle there hands the
// pages back to the guest allocator while the host still writes them, which corrupts
// unrelated guest memory. Retire the resource instead: it is released once, at process exit.
// The host already keeps the matching AHB reference for the same reason.
static bool cib1DiagEnabled() {
    static const bool enabled = [] {
        const char* v = getenv("GFXSTREAM_CIB_ALLOC_DIAG");
        return v && v[0] && strcmp(v, "0") != 0;
    }();
    return enabled;
}
static void cib1Retire(VirtGpuResourcePtr& resource) {
    if (!resource) return;
    const uint32_t handle = resource->getResourceHandle();
    static std::vector<VirtGpuResourcePtr>* retired =
        new std::vector<VirtGpuResourcePtr>();  // process lifetime by design
    retired->push_back(std::move(resource));
    static std::atomic<uint32_t> retiredCount{0};
    const uint32_t count = retiredCount.fetch_add(1, std::memory_order_relaxed) + 1;
    if (cib1DiagEnabled() && (count <= 64 || (count % 64) == 0)) {
        mesa_logw("CIB1-RETIRE: resource=%u retired=%u kept-until-exit", handle, count);
    }
}
#endif

'''
edit(rt, 'static constexpr uint32_t kDefaultApiVersion', helpers + 'static constexpr uint32_t kDefaultApiVersion')
# Make bridge creation transactional: local shared_ptr owns GEM until image table commit.
edit(rt, '''    vk_struct_chain_iterator structChainIter = vk_make_chain_iterator(&localCreateInfo);
    VkExternalMemoryImageCreateInfo localExtImgCi;''', '''    vk_struct_chain_iterator structChainIter = vk_make_chain_iterator(&localCreateInfo);
    VkExternalMemoryImageCreateInfo localExtImgCi;
    bool cib1 = false;
    (void)cib1;
#if defined(LINUX_GUEST_BUILD)
    VirtGpuResourcePtr cib1Resource;
    uint32_t cib1ResourceId = 0;
    VkNativeBufferANDROID cib1Native{};
    VkImageFormatListCreateInfo cib1ViewList{};
    VkFormat cib1ViewFormats[2] = {};
#endif''')
edit(rt, '''    if (extImgCiPtr) {
        localExtImgCi = vk_make_orphan_copy(*extImgCiPtr);
        vk_append_struct(&structChainIter, &localExtImgCi);
    }

#if defined(LINUX_GUEST_BUILD)''', '''#if defined(LINUX_GUEST_BUILD)
    const auto* cib1ViewsInfo = vk_find_struct_const(pCreateInfo, IMAGE_FORMAT_LIST_CREATE_INFO);
    const auto* cib1Modifier = vk_find_struct_const(pCreateInfo, IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT);
    const auto* cib1Explicit = vk_find_struct_const(pCreateInfo, IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT);
    // DRI3/dmabuf imports arrive as DRM_FORMAT_MODIFIER_EXT with an explicit LINEAR modifier and
    // its plane layout. The base emulation path already reduces those to a LINEAR wire image; the
    // bridge can stand in for the same images, it just has to recognise the explicit form too.
    const bool cib1ExplicitLinear = cib1Explicit &&
        cib1Explicit->drmFormatModifier == DRM_FORMAT_MOD_LINEAR &&
        cib1Explicit->drmFormatModifierPlaneCount == 1 && cib1Explicit->pPlaneLayouts &&
        cib1Explicit->pPlaneLayouts[0].offset == 0;
    const bool cib1Linear = pCreateInfo->tiling == VK_IMAGE_TILING_LINEAR ||
        (pCreateInfo->tiling == VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT &&
         ((cib1Modifier && cib1Modifier->drmFormatModifierCount == 1 &&
           cib1Modifier->pDrmFormatModifiers &&
           cib1Modifier->pDrmFormatModifiers[0] == DRM_FORMAT_MOD_LINEAR) ||
          cib1ExplicitLinear));
    // Keep DMA_BUF (WSI/GBM scanout) images on their imported guest memory unless
    // explicitly requested. The bridge's host ColorBuffer cannot be the guest's
    // scanout buffer, so converting it disconnects the compositor output.
    const bool cib1DmabufAllowed = getenv("GFXSTREAM_CIB_ALLOW_DMABUF") != nullptr;
    const bool cib1DmabufExcluded = extImgCiPtr &&
        (extImgCiPtr->handleTypes & VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT) &&
        !cib1DmabufAllowed;
    cib1 = mCaps.classicImageBridgeAck && extImgCiPtr &&
        cib1Handles(extImgCiPtr->handleTypes) && cib1Linear &&
        !cib1DmabufExcluded &&
        cib1Views(pCreateInfo->format, pCreateInfo->flags, cib1ViewsInfo) &&
        pCreateInfo->sharingMode == VK_SHARING_MODE_EXCLUSIVE &&
        pCreateInfo->initialLayout == VK_IMAGE_LAYOUT_UNDEFINED &&
        (!cib1Explicit || cib1Explicit->drmFormatModifier == DRM_FORMAT_MOD_LINEAR) &&
        cib1Geometry(pCreateInfo->imageType, pCreateInfo->format, pCreateInfo->flags,
            pCreateInfo->usage, pCreateInfo->extent.depth, pCreateInfo->mipLevels,
            pCreateInfo->arrayLayers, pCreateInfo->samples);
    if (cib1) {
        const uint32_t width = pCreateInfo->extent.width, height = pCreateInfo->extent.height;
        uint32_t stride = 0, bytes = 0;
        if (!cib1Layout(width, height, &stride, &bytes)) return VK_ERROR_FORMAT_NOT_SUPPORTED;
        // The importer's plane layout is the contract the buffer was handed over with; the classic
        // image can only stand in for it when the strides agree.
        if (cib1Explicit && cib1Explicit->pPlaneLayouts[0].rowPitch != stride)
            return VK_ERROR_FORMAT_NOT_SUPPORTED;
        auto* gpu = VirtGpuDevice::getInstance();
        if (!gpu) return VK_ERROR_INITIALIZATION_FAILED;
        const uint32_t vf = gfxstream::vk::getVirglFormat(pCreateInfo->format);
        if (!vf) return VK_ERROR_FORMAT_NOT_SUPPORTED;
        cib1Resource = gpu->createResource(width, height, stride, bytes, vf,
            PIPE_TEXTURE_2D, VIRGL_BIND_RENDER_TARGET | VIRGL_BIND_SCANOUT | VIRGL_BIND_SHARED);
        if (!cib1Resource || cib1Resource->wait()) return VK_ERROR_OUT_OF_DEVICE_MEMORY;
        cib1ResourceId = cib1Resource->getResourceHandle();
        if (!cib1ResourceId) return VK_ERROR_INITIALIZATION_FAILED;
        localCreateInfo.tiling = VK_IMAGE_TILING_LINEAR;\n        cib1Native.sType = VK_STRUCTURE_TYPE_NATIVE_BUFFER_ANDROID;
        cib1Native.handle = &cib1ResourceId;
        cib1Native.stride = static_cast<int>(stride / 4);
        cib1Native.format = pCreateInfo->format == VK_FORMAT_R8G8B8A8_UNORM ? 1 : 5;
        cib1Native.usage = 0;
        cib1Native.usage2.consumer = UINT64_C(0x4349423100000001);
        cib1Native.usage2.producer = 0;
        vk_append_struct(&structChainIter, &cib1Native);
        if (cib1ViewsInfo) {
            cib1ViewList = vk_make_orphan_copy(*cib1ViewsInfo);
            vk_append_struct(&structChainIter, &cib1ViewList);
        } else if (pCreateInfo->flags & VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT) {
            // No list was supplied. Synthesize only the wire list; the application's
            // original create info is unchanged. The host and driver still validate.
            const VkFormat srgb = pCreateInfo->format == VK_FORMAT_R8G8B8A8_UNORM
                ? VK_FORMAT_R8G8B8A8_SRGB : VK_FORMAT_B8G8R8A8_SRGB;
            cib1ViewFormats[0] = pCreateInfo->format;
            cib1ViewFormats[1] = srgb;
            cib1ViewList.sType = VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO;
            cib1ViewList.pNext = nullptr;
            cib1ViewList.viewFormatCount = 2;
            cib1ViewList.pViewFormats = cib1ViewFormats;
            vk_append_struct(&structChainIter, &cib1ViewList);
        }
    }
    if (extImgCiPtr && !cib1) {
#else
    if (extImgCiPtr) {
#endif
        localExtImgCi = vk_make_orphan_copy(*extImgCiPtr);
        vk_append_struct(&structChainIter, &localExtImgCi);
    }

#if defined(LINUX_GUEST_BUILD)''')
# Native marker replaces modifier pNext on wire, original metadata retained below.
edit(rt, '''    if ((pCreateInfo->tiling == VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT) &&
        (drmFmtMod || drmFmtModList)) {''', '''    if (!cib1 && (pCreateInfo->tiling == VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT) &&
        (drmFmtMod || drmFmtModList)) {''')
# Marker host owns true native tiling; LINEAR remains guest exported staging layout.
edit(rt, '''    if (res != VK_SUCCESS) return res;

    std::lock_guard<std::recursive_mutex> lock(mLock);

    auto it = info_VkImage.find(*pImage);''', '''    if (res != VK_SUCCESS) return res;
#if defined(LINUX_GUEST_BUILD)
    if (cib1Resource) {
        enc->vkGetImageMemoryRequirements(device, *pImage, &memReqs, true /* do lock */);
        const auto& props = getPhysicalDeviceMemoryProperties(context, device, VK_NULL_HANDLE);
        uint32_t bits = 0;
        for (uint32_t i = 0; i < props.memoryTypeCount; ++i) {
            const auto flags = props.memoryTypes[i].propertyFlags;
            if ((memReqs.memoryTypeBits & (1u << i)) &&
                flags == VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) bits |= 1u << i;
        }
        if (!memReqs.size || memReqs.alignment != 1 || !bits) {
            enc->vkDestroyImage(device, *pImage, pAllocator, true /* do lock */);
            *pImage = VK_NULL_HANDLE;
            return VK_ERROR_FORMAT_NOT_SUPPORTED;
        }
        memReqs.memoryTypeBits = bits;
    }
#endif
    std::lock_guard<std::recursive_mutex> lock(mLock);

    auto it = info_VkImage.find(*pImage);''')
edit(rt, '''    info.createInfo = *pCreateInfo;
    info.createInfo.pNext = nullptr;

#ifdef VK_USE_PLATFORM_ANDROID_KHR''', '''    info.createInfo = *pCreateInfo;
    info.createInfo.pNext = nullptr;
#if defined(LINUX_GUEST_BUILD)
    if (cib1Resource) {
        info.classicBridgeResource = std::move(cib1Resource);
        info.baseRequirementsKnown = true;
        info.baseRequirements = memReqs;
    }
#endif

#ifdef VK_USE_PLATFORM_ANDROID_KHR''')
edit(rt, '''    if (extImgCiPtr &&
        (extImgCiPtr->handleTypes & VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT)) {
        updateMemoryTypeBits(&memReqs.memoryTypeBits, mCaps.vulkanCapset.colorBufferMemoryIndex);
    }''', '''    if (!info.classicBridgeResource && extImgCiPtr &&
        (extImgCiPtr->handleTypes & VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT)) {
        updateMemoryTypeBits(&memReqs.memoryTypeBits, mCaps.vulkanCapset.colorBufferMemoryIndex);
    }''')
# Bridge requirements never invoke the unrelated 64x64 color-buffer type heuristic.
edit(rt, '''#if defined(LINUX_GUEST_BUILD)
    if (mCaps.vulkanCapset.colorBufferMemoryIndex == 0xFFFFFFFF) {
        mCaps.vulkanCapset.colorBufferMemoryIndex = getColorBufferMemoryIndex(context, device);
    }
    if (!info.classicBridgeResource''', '''#if defined(LINUX_GUEST_BUILD)
    if (!info.classicBridgeResource && mCaps.vulkanCapset.colorBufferMemoryIndex == 0xFFFFFFFF) {
        mCaps.vulkanCapset.colorBufferMemoryIndex = getColorBufferMemoryIndex(context, device);
    }
    if (!info.classicBridgeResource''')
# Allocation bypasses ordinary hostvisible/export paths, uses real CB import once.
edit(rt, '''    bool hasDedicatedImage = false;
    bool hasDedicatedBuffer = false;

    VkMemoryAllocateInfo finalAllocInfo''', '''#if defined(LINUX_GUEST_BUILD)
    const auto* cib1Dedicated = vk_find_struct_const(pAllocateInfo, MEMORY_DEDICATED_ALLOCATE_INFO);
    if (cib1Dedicated && cib1Dedicated->image) {
        std::unique_lock<std::recursive_mutex> lock(mLock);
        auto image = info_VkImage.find(cib1Dedicated->image);
        if (image != info_VkImage.end() && image->second.classicBridgeResource) {
            auto& ci = image->second;
            const auto* exportInfo = vk_find_struct_const(pAllocateInfo, EXPORT_MEMORY_ALLOCATE_INFO);
            const auto* importFdInfo = vk_find_struct_const(pAllocateInfo, IMPORT_MEMORY_FD_INFO_KHR);
            // The bridge owns the ColorBuffer backing. A compositor allocation that names
            // neither VkExportMemoryAllocateInfo nor VkImportMemoryFdInfoKHR still has to bind
            // to it, so export/import metadata is informational only and must not skip the
            // ColorBuffer import; the fd is never sent to the host. A genuine mismatch below
            // still fails closed.
            const bool cib1Eligible =
                cib1BridgeClaim(image != info_VkImage.end(), ci.device == device,
                                ci.classicBridgeAllocated,
                                cib1Dedicated->buffer != VK_NULL_HANDLE,
                                pAllocateInfo->allocationSize == ci.baseRequirements.size,
                                pAllocateInfo->memoryTypeIndex < 32 &&
                                    (ci.baseRequirements.memoryTypeBits &
                                     (1u << pAllocateInfo->memoryTypeIndex)));
            if (cib1DiagEnabled()) {
                static std::atomic<uint32_t> sCib1AllocDiagLogs{0};
                if (sCib1AllocDiagLogs.fetch_add(1, std::memory_order_relaxed) < 64) {
                    mesa_logw(
                        "CIB1-ALLOC-GUEST: image=%p bridge=1 dedicated=1 export=0x%x importFd=%d "
                        "size=%llu req=%llu index=%u bits=0x%x decided=%s",
                        (void*)cib1Dedicated->image, exportInfo ? exportInfo->handleTypes : 0,
                        importFdInfo ? importFdInfo->fd : -1,
                        (unsigned long long)pAllocateInfo->allocationSize,
                        (unsigned long long)ci.baseRequirements.size,
                        pAllocateInfo->memoryTypeIndex, ci.baseRequirements.memoryTypeBits,
                        cib1Eligible ? "bridge" : "reject");
                }
            }
            if (!cib1Eligible) return VK_ERROR_INVALID_EXTERNAL_HANDLE;
            auto owner = ci.classicBridgeResource;
            ci.classicBridgeAllocated = true;
            VkMemoryAllocateInfo local = *pAllocateInfo;
            auto dedicated = vk_make_orphan_copy(*cib1Dedicated);
            VkImportColorBufferGOOGLE cb{VK_STRUCTURE_TYPE_IMPORT_COLOR_BUFFER_GOOGLE,
                nullptr, owner->getResourceHandle()};
            local.pNext = &dedicated;
            dedicated.pNext = &cb;
            lock.unlock();
            const VkResult result = enc->vkAllocateMemory(device, &local, pAllocator, pMemory, true);
            lock.lock();
            image = info_VkImage.find(cib1Dedicated->image);
            if (result != VK_SUCCESS) {
                if (image != info_VkImage.end()) {
                    image->second.classicBridgeAllocated = false;
                    image->second.classicBridgeMemory = VK_NULL_HANDLE;
                }
                return result;
            }
            // Valid Vulkan callers keep the image alive through allocation. Retain GEM
            // regardless; defensively roll back native success if image identity disappeared.
            if (!cib1RetainAllocation(image != info_VkImage.end(),
                    image != info_VkImage.end() && image->second.classicBridgeResource == owner,
                    result == VK_SUCCESS)) {
                if (image != info_VkImage.end()) {
                    image->second.classicBridgeAllocated = false;
                    image->second.classicBridgeMemory = VK_NULL_HANDLE;
                }
                lock.unlock();
                enc->vkFreeMemory(device, *pMemory, pAllocator, true);
                *pMemory = VK_NULL_HANDLE;
                return VK_ERROR_INITIALIZATION_FAILED;
            }
            setDeviceMemoryInfo(device, *pMemory, pAllocateInfo->allocationSize, nullptr,
                pAllocateInfo->memoryTypeIndex, nullptr, false, ZX_HANDLE_INVALID, owner, -1);
            info_VkDeviceMemory[*pMemory].classicBridgeImage = cib1Dedicated->image;
            image->second.classicBridgeMemory = *pMemory;
            return VK_SUCCESS;
        }
    }
#endif
    bool hasDedicatedImage = false;
    bool hasDedicatedBuffer = false;

    VkMemoryAllocateInfo finalAllocInfo''')
# Both requirement entrypoints must serve same authoritative cached virtual contract.
edit(rt, '''    VkEncoder* enc = (VkEncoder*)context;
    enc->vkGetImageMemoryRequirements2(device, pInfo, pMemoryRequirements, true /* do lock */);''', '''#if defined(LINUX_GUEST_BUILD)
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        auto image = info_VkImage.find(pInfo->image);
        if (image != info_VkImage.end() && image->second.classicBridgeResource) {
            pMemoryRequirements->memoryRequirements = image->second.baseRequirements;
            if (auto* dedicated = vk_find_struct(pMemoryRequirements, MEMORY_DEDICATED_REQUIREMENTS)) {
                dedicated->prefersDedicatedAllocation = VK_TRUE;
                dedicated->requiresDedicatedAllocation = VK_TRUE;
            }
            return;
        }
    }
#endif
    VkEncoder* enc = (VkEncoder*)context;
    enc->vkGetImageMemoryRequirements2(device, pInfo, pMemoryRequirements, true /* do lock */);''')
# Simple bind route must reject wrong offset/resource and duplicate bind.
edit(rt, '''    return enc->vkBindImageMemory(device, image, memory, memoryOffset, true /* do lock */);''', '''#if defined(LINUX_GUEST_BUILD)
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        auto imageIt = info_VkImage.find(image);
        auto memoryIt = info_VkDeviceMemory.find(memory);
        if (imageIt == info_VkImage.end() || memoryIt == info_VkDeviceMemory.end())
            return VK_ERROR_OUT_OF_DEVICE_MEMORY;
        auto& ii = imageIt->second;
        auto& mi = memoryIt->second;
        if (ii.classicBridgeResource || mi.classicBridgeImage) {
            if (!cib1BindIdentity(ii.classicBridgeResource != nullptr, mi.classicBridgeImage != VK_NULL_HANDLE,
                    mi.classicBridgeImage == image, mi.blobPtr == ii.classicBridgeResource,
                    ii.classicBridgeMemory == memory, mi.classicBridgeBound, memoryOffset))
                return VK_ERROR_INVALID_EXTERNAL_HANDLE;
            const auto result = enc->vkBindImageMemory(device, image, memory, 0, true);
            if (result == VK_SUCCESS) mi.classicBridgeBound = true;
            return result;
        }
    }
#endif
    return enc->vkBindImageMemory(device, image, memory, memoryOffset, true /* do lock */);''')
# Bind2 uses validated single-bind helper for bridge, does not silently bypass checks.
edit(rt, '''    return enc->vkBindImageMemory2(device, bindingCount, pBindInfos, true /* do lock */);''', '''#if defined(LINUX_GUEST_BUILD)
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        for (uint32_t i = 0; i < bindingCount; ++i) {
            const auto image = info_VkImage.find(pBindInfos[i].image);
            const auto memory = info_VkDeviceMemory.find(pBindInfos[i].memory);
            const bool bridge = (image != info_VkImage.end() && image->second.classicBridgeResource) ||
                (memory != info_VkDeviceMemory.end() && memory->second.classicBridgeImage);
            if (bridge) {
                if (bindingCount != 1 || pBindInfos[i].pNext) return VK_ERROR_INVALID_EXTERNAL_HANDLE;
                return on_vkBindImageMemory(context, VK_SUCCESS, device, pBindInfos[i].image,
                    pBindInfos[i].memory, pBindInfos[i].memoryOffset);
            }
        }
    }
#endif
    return enc->vkBindImageMemory2(device, bindingCount, pBindInfos, true /* do lock */);''')
# CIB1 free releases the allocation claim once and retires the GEM resource instead of
# closing it synchronously while the host may still be draining work into it.
edit(rt, '''    if (!info.coherentMemory) {
        lock.unlock();
        VkEncoder* enc = (VkEncoder*)context;
        enc->vkFreeMemory(device, memory, pAllocateInfo, true /* do lock */);
        return;
    }''', '''#if defined(LINUX_GUEST_BUILD)
    if (info.classicBridgeImage) {
        // Clear the allocation claim before the host free. The GEM resource is retired, not
        // released: the image owns it and the host may still be draining work that writes
        // into the ColorBuffer backed by these pages.
        auto bridgeImage = info_VkImage.find(info.classicBridgeImage);
        if (cib1BridgeRelease(bridgeImage != info_VkImage.end(),
                              bridgeImage != info_VkImage.end() &&
                                  bridgeImage->second.classicBridgeMemory == memory,
                              true)) {
            bridgeImage->second.classicBridgeMemory = VK_NULL_HANDLE;
            bridgeImage->second.classicBridgeAllocated = false;
        }
        info.classicBridgeImage = VK_NULL_HANDLE;
        info.classicBridgeBound = false;
        cib1Retire(info.blobPtr);
    }
#endif
    if (!info.coherentMemory) {
        lock.unlock();
        VkEncoder* enc = (VkEncoder*)context;
        enc->vkFreeMemory(device, memory, pAllocateInfo, true /* do lock */);
        // The encoder's destroy mapping normally unregisters the handle inside the call.
        // Erase again so the tracker state is idempotent if that mapping is skipped and a
        // recycled handle can never observe a stale bridge entry.
        unregister_VkDeviceMemory(memory);
        return;
    }''')
# CIB1 image destroy clears the paired memory's bridge identity and retires the resource.
edit(rt, '''    VkEncoder* enc = (VkEncoder*)context;
    enc->vkDestroyImage(device, image, pAllocator, true /* do lock */);
}''', '''#if defined(LINUX_GUEST_BUILD)
    {
        std::lock_guard<std::recursive_mutex> lock(mLock);
        auto bridgeImage = info_VkImage.find(image);
        if (bridgeImage != info_VkImage.end() && bridgeImage->second.classicBridgeResource) {
            auto& ii = bridgeImage->second;
            if (ii.classicBridgeMemory != VK_NULL_HANDLE) {
                auto bridgeMemory = info_VkDeviceMemory.find(ii.classicBridgeMemory);
                if (bridgeMemory != info_VkDeviceMemory.end()) {
                    bridgeMemory->second.classicBridgeImage = VK_NULL_HANDLE;
                    bridgeMemory->second.classicBridgeBound = false;
                }
                ii.classicBridgeMemory = VK_NULL_HANDLE;
            }
            ii.classicBridgeAllocated = false;
            // Keep the staging GEM resource alive past the host destroy; see cib1Retire().
            cib1Retire(ii.classicBridgeResource);
        }
    }
#endif
    VkEncoder* enc = (VkEncoder*)context;
    enc->vkDestroyImage(device, image, pAllocator, true /* do lock */);
}''')
# CIB query chain is copied to known local nodes before baseline filter can mutate it.
edit(rt, '''    VkPhysicalDeviceImageFormatInfo2 localImageFormatInfo = *pImageFormatInfo;

    uint32_t supportedHandleType = 0;''', '''    VkPhysicalDeviceImageFormatInfo2 localImageFormatInfo = *pImageFormatInfo;
#if defined(LINUX_GUEST_BUILD)
    VkPhysicalDeviceExternalImageFormatInfo cib1LocalExternal{};
    VkPhysicalDeviceImageDrmFormatModifierInfoEXT cib1LocalModifier{};
    VkImageFormatListCreateInfo cib1LocalViews{};
    bool cib1ChainKnown = true;
    for (auto* next = static_cast<const VkBaseInStructure*>(pImageFormatInfo->pNext);
         next; next = next->pNext) {
        if (next->sType != VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO &&
            next->sType != VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_DRM_FORMAT_MODIFIER_INFO_EXT &&
            next->sType != VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO) cib1ChainKnown = false;
    }
    if (mCaps.classicImageBridgeAck && cib1ChainKnown) {
        localImageFormatInfo.pNext = nullptr;
        if (const auto* views = vk_find_struct_const(pImageFormatInfo, IMAGE_FORMAT_LIST_CREATE_INFO)) {
            cib1LocalViews = *views;
            cib1LocalViews.pNext = localImageFormatInfo.pNext;
            localImageFormatInfo.pNext = &cib1LocalViews;
        }
        if (const auto* ext = vk_find_struct_const(pImageFormatInfo, PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO)) {
            cib1LocalExternal = *ext;
            cib1LocalExternal.pNext = localImageFormatInfo.pNext;
            localImageFormatInfo.pNext = &cib1LocalExternal;
        }
        if (const auto* mod = vk_find_struct_const(pImageFormatInfo, PHYSICAL_DEVICE_IMAGE_DRM_FORMAT_MODIFIER_INFO_EXT)) {
            cib1LocalModifier = *mod;
            cib1LocalModifier.pNext = localImageFormatInfo.pNext;
            localImageFormatInfo.pNext = &cib1LocalModifier;
        }
    }
#endif
    uint32_t supportedHandleType = 0;''')
# Query only promises export of the exact requested handle type. Host performs real
# bridge format/usage validation; never convert a failed query to success.
edit(rt, '''    uint32_t supportedHandleType = 0;
    VkExternalImageFormatProperties* ext_img_properties =''', '''    bool cib1Query = false;
#if defined(LINUX_GUEST_BUILD)
    const auto* cib1Ext = vk_find_struct_const(pImageFormatInfo, PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO);
    const auto* cib1Mod = vk_find_struct_const(pImageFormatInfo, PHYSICAL_DEVICE_IMAGE_DRM_FORMAT_MODIFIER_INFO_EXT);
    const auto* cib1ViewInfo = vk_find_struct_const(pImageFormatInfo, IMAGE_FORMAT_LIST_CREATE_INFO);
    const bool cib1QueryLinear = pImageFormatInfo->tiling == VK_IMAGE_TILING_LINEAR ||
        (pImageFormatInfo->tiling == VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT && cib1Mod &&
         cib1Mod->drmFormatModifier == DRM_FORMAT_MOD_LINEAR);
    cib1Query = mCaps.classicImageBridgeAck && cib1ChainKnown && cib1Ext &&
        (cib1Ext->handleType == VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT ||
         cib1Ext->handleType == VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT) &&
        cib1QueryLinear && cib1Views(pImageFormatInfo->format, pImageFormatInfo->flags, cib1ViewInfo) &&
        cib1Geometry(pImageFormatInfo->type, pImageFormatInfo->format,
            pImageFormatInfo->flags, pImageFormatInfo->usage, 1, 1, 1, VK_SAMPLE_COUNT_1_BIT);
#endif
    (void)cib1Query;
    uint32_t supportedHandleType = 0;
    VkExternalImageFormatProperties* ext_img_properties =''')
edit(rt, '''                localImageFormatInfo.usage &=
                    ~(VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_INPUT_ATTACHMENT_BIT);''', '''                if (!cib1Query) localImageFormatInfo.usage &=
                    ~(VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_INPUT_ATTACHMENT_BIT);''')
edit(rt, '''    if (hostRes != VK_SUCCESS) return hostRes;

#ifdef LINUX_GUEST_BUILD''', '''    if (hostRes != VK_SUCCESS) return hostRes;
#if defined(LINUX_GUEST_BUILD)
    if (cib1Query) {
        if (!ext_img_properties ||
            ext_img_properties->externalMemoryProperties.externalMemoryFeatures !=
                (VK_EXTERNAL_MEMORY_FEATURE_EXPORTABLE_BIT | VK_EXTERNAL_MEMORY_FEATURE_DEDICATED_ONLY_BIT) ||
            ext_img_properties->externalMemoryProperties.exportFromImportedHandleTypes ||
            ext_img_properties->externalMemoryProperties.compatibleHandleTypes != cib1Ext->handleType)
            return VK_ERROR_FORMAT_NOT_SUPPORTED;
        pImageFormatProperties->imageFormatProperties.maxMipLevels = 1;
        pImageFormatProperties->imageFormatProperties.maxArrayLayers = 1;
        pImageFormatProperties->imageFormatProperties.sampleCounts = VK_SAMPLE_COUNT_1_BIT;
        return VK_SUCCESS; // Export-only CIB1; bypass old unconditional synthetic DMA properties.
    }
#endif

#ifdef LINUX_GUEST_BUILD''')
# Export actual guest GEM only after its dedicated native bind succeeded.
edit(rt, '''    if (!info.blobPtr) {
        mesa_loge("%s: VkDeviceMemory does not have a resource available for export.\\n", __func__);''', '''    if (info.classicBridgeImage && !info.classicBridgeBound)
        return VK_ERROR_INVALID_EXTERNAL_HANDLE;
    if (!info.blobPtr) {
        mesa_loge("%s: VkDeviceMemory does not have a resource available for export.\\n", __func__);''')
edit(rt, '''    auto& deviceMemoryInfo = deviceMemoryInfoIt->second;

    // The CreateGuestHandle capability''', '''    auto& deviceMemoryInfo = deviceMemoryInfoIt->second;
    if (deviceMemoryInfo.classicBridgeImage) return VK_ERROR_MEMORY_MAP_FAILED;

    // The CreateGuestHandle capability''')
edit(rt, '''    hostRes = enc->vkGetPhysicalDeviceImageFormatProperties2(
        physicalDevice, &localImageFormatInfo, pImageFormatProperties, true /* do lock */);''', '''#if defined(LINUX_GUEST_BUILD)
    VkNativeBufferANDROID cib1QueryMarker{};
    if (cib1Query) {
        cib1QueryMarker.sType = VK_STRUCTURE_TYPE_NATIVE_BUFFER_ANDROID;
        cib1QueryMarker.pNext = localImageFormatInfo.pNext;
        cib1QueryMarker.usage2.consumer = UINT64_C(0x4349423100000001);
        localImageFormatInfo.pNext = &cib1QueryMarker;
    }
#endif
    hostRes = enc->vkGetPhysicalDeviceImageFormatProperties2(
        physicalDevice, &localImageFormatInfo, pImageFormatProperties, true /* do lock */);''')
print('Applied CIB1 core draft; parent review REQUIRED; export-only, no bridge FD imports')
