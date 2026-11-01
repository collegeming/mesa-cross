#!/usr/bin/env python3
"""Actions-only extract exact production helpers, compile/run CPU tests, no Vulkan linkage."""
import pathlib
import subprocess
import sys
root = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2]).resolve()
source = (root / 'src/gfxstream/guest/vulkan_enc/ResourceTracker.cpp').read_text()
start = source.index('static bool cib1Geometry(')
end = source.index('#endif', start)
helpers = source[start:end]
assert 'static bool cib1Handles' in helpers
cpp = r'''
#include <vulkan/vulkan.h>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <limits>
#include <cstdio>
#include "virtio/virtio-gpu/virtgpu_gfxstream_protocol.h"
'''+helpers+r'''
int main() {
    vulkanCapset full{}; full.classicImageBridgeVersion=1; full.classicImageBridgeContract=0x43494231u;
    for (unsigned n=0;n<=128;++n) {
        vulkanCapset got{}; std::memcpy(&got,&full,n);
        assert(cib1CapAcknowledged(0,got) == (n==128));
        assert(!cib1CapAcknowledged(-1,got));
    }
    full.classicImageBridgeVersion=2; assert(!cib1CapAcknowledged(0,full));
    full.classicImageBridgeVersion=1; full.classicImageBridgeContract=0; assert(!cib1CapAcknowledged(0,full));
    const auto usage=VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT|VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    assert(cib1Geometry(VK_IMAGE_TYPE_2D,VK_FORMAT_R8G8B8A8_UNORM,VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT,usage,1,1,1,VK_SAMPLE_COUNT_1_BIT));
    assert(cib1Geometry(VK_IMAGE_TYPE_2D,VK_FORMAT_B8G8R8A8_UNORM,VK_IMAGE_CREATE_ALIAS_BIT,usage,1,1,1,VK_SAMPLE_COUNT_1_BIT));
    assert(!cib1Geometry(VK_IMAGE_TYPE_3D,VK_FORMAT_R8G8B8A8_UNORM,0,usage,1,1,1,VK_SAMPLE_COUNT_1_BIT));
    assert(!cib1Geometry(VK_IMAGE_TYPE_2D,VK_FORMAT_R8G8B8A8_SRGB,0,usage,1,1,1,VK_SAMPLE_COUNT_1_BIT));
    assert(!cib1Geometry(VK_IMAGE_TYPE_2D,VK_FORMAT_R8G8B8A8_UNORM,VK_IMAGE_CREATE_EXTENDED_USAGE_BIT,usage,1,1,1,VK_SAMPLE_COUNT_1_BIT));
    assert(!cib1Geometry(VK_IMAGE_TYPE_2D,VK_FORMAT_R8G8B8A8_UNORM,0,VK_IMAGE_USAGE_STORAGE_BIT,1,1,1,VK_SAMPLE_COUNT_1_BIT));
    assert(!cib1Geometry(VK_IMAGE_TYPE_2D,VK_FORMAT_R8G8B8A8_UNORM,0,usage,1,2,1,VK_SAMPLE_COUNT_1_BIT));
    assert(!cib1Geometry(VK_IMAGE_TYPE_2D,VK_FORMAT_R8G8B8A8_UNORM,0,usage,1,1,2,VK_SAMPLE_COUNT_1_BIT));
    assert(!cib1Geometry(VK_IMAGE_TYPE_2D,VK_FORMAT_R8G8B8A8_UNORM,0,usage,1,1,1,VK_SAMPLE_COUNT_2_BIT));
    assert(!cib1Geometry(VK_IMAGE_TYPE_2D,VK_FORMAT_R8G8B8A8_UNORM,0,0,1,1,1,VK_SAMPLE_COUNT_1_BIT));
    VkFormat formats[]={VK_FORMAT_R8G8B8A8_UNORM,VK_FORMAT_R8G8B8A8_SRGB};
    VkImageFormatListCreateInfo list{VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO,nullptr,2,formats};
    assert(cib1Views(VK_FORMAT_R8G8B8A8_UNORM,VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT,&list));
    assert(!cib1Views(VK_FORMAT_B8G8R8A8_UNORM,VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT,&list));
    assert(!cib1Views(VK_FORMAT_R8G8B8A8_UNORM,0,&list));
    formats[1]=formats[0]; assert(!cib1Views(VK_FORMAT_R8G8B8A8_UNORM,VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT,&list));
    formats[1]=VK_FORMAT_B8G8R8A8_SRGB; assert(!cib1Views(VK_FORMAT_R8G8B8A8_UNORM,VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT,&list));
    formats[0]=VK_FORMAT_B8G8R8A8_UNORM; assert(cib1Views(VK_FORMAT_B8G8R8A8_UNORM,VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT,&list));
    list.viewFormatCount=3; assert(!cib1Views(VK_FORMAT_B8G8R8A8_UNORM,VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT,&list));
    list.viewFormatCount=0; assert(!cib1Views(VK_FORMAT_B8G8R8A8_UNORM,VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT,&list));
    assert(cib1Views(VK_FORMAT_B8G8R8A8_UNORM,VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT,nullptr));
    assert(cib1Views(VK_FORMAT_R8G8B8A8_UNORM,VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT,nullptr));
    assert(cib1Views(VK_FORMAT_B8G8R8A8_UNORM,0,nullptr));
    uint32_t stride=0,size=0;
    assert(cib1Layout(1280,720,&stride,&size)&&stride==5120&&size==3686400);
    assert(cib1Layout(1,1,&stride,&size)&&stride==16&&size==16);
    assert(cib1Layout(5,2,&stride,&size)&&stride==32&&size==64);
    assert(!cib1Layout(0,1,&stride,&size)); assert(!cib1Layout(1,0,&stride,&size));
    assert(!cib1Layout(UINT32_MAX,1,&stride,&size)); assert(!cib1Layout(65536,65536,&stride,&size));
    for(unsigned mask=0;mask<32;++mask) {
        bool image=mask&1,memory=mask&2,same=mask&4,owner=mask&8,bound=mask&16;
        assert(cib1BindIdentity(image,memory,same,owner,bound,0)==(mask==15));
        assert(!cib1BindIdentity(image,memory,same,owner,bound,1));
    }
    for(unsigned mask=0;mask<8;++mask)
        assert(cib1RetainAllocation(mask&1,mask&2,mask&4)==(mask==7));
    assert(!cib1Handles(0)); assert(cib1Handles(VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT));
    assert(cib1Handles(VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT));
    assert(!cib1Handles(VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID));
    puts("PASS extracted production CIB1 helpers:129 short/full cap copies, failure/version/default-off; geometry/mutable view families incl no-list MUTABLE; overflow/layout;32 bind identity states;8 allocation retention/rollback decisions; no Vulkan linkage or GPU execution");
}
'''
out.mkdir(parents=True, exist_ok=True)
path = out / 'classic-image-bridge-helper-test.cpp'
path.write_text(cpp)
exe = out / 'classic-image-bridge-helper-test'
subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror','-O1','-g','-fno-omit-frame-pointer',
                '-fsanitize=address,undefined','-fno-sanitize-recover=all','-I'+str(root/'include'),
                '-I'+str(root/'src'),str(path),'-o',str(exe)],check=True)
imports = subprocess.check_output(['nm','-u',str(exe)],text=True)
assert not any(' vk' in line for line in imports.splitlines())
(out/'helper-imports.txt').write_text(imports)
subprocess.run([str(exe)],check=True)
exe.unlink()
