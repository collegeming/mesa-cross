/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * mb-mem-probe: a controlled Vulkan client for the MaliBridge memory-budget tests.
 * It asks the server for exactly the allocation the test names and prints the VkResult, so a
 * refusal can be told from a driver failure without changing the server under test.
 *
 *   mb-mem-probe --alloc MB [--repeat N] [--keep] [--hold S]
 *   mb-mem-probe --image WxH [--repeat N] [--keep] [--hold S]
 *
 * Loads the system Vulkan loader (libvulkan.so.1) and needs VKBRIDGE_SOCKET and
 * VK_DRIVER_FILES/VK_ICD_FILENAMES pointing at the bridge, as any Linux client does.
 */
#define _GNU_SOURCE
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan.h>

#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static PFN_vkGetInstanceProcAddr gipa;

static void *load_vulkan(void)
{
    const char *names[] = {"libvulkan.so.1", "libvulkan.so", NULL};
    for (int i = 0; names[i]; i++) {
        void *h = dlopen(names[i], RTLD_NOW | RTLD_LOCAL);
        if (h) {
            gipa = (PFN_vkGetInstanceProcAddr)dlsym(h, "vkGetInstanceProcAddr");
            if (gipa) return h;
            dlclose(h);
        }
    }
    return NULL;
}

static uint32_t pick_type(const VkPhysicalDeviceMemoryProperties *mp, uint32_t bits, VkMemoryPropertyFlags want)
{
    for (uint32_t i = 0; i < mp->memoryTypeCount; i++)
        if ((bits & (1u << i)) && (mp->memoryTypes[i].propertyFlags & want) == want) return i;
    for (uint32_t i = 0; i < mp->memoryTypeCount; i++)
        if (bits & (1u << i)) return i;
    return UINT32_MAX;
}

int main(int argc, char **argv)
{
    unsigned long alloc_mb = 64, repeat = 1;
    unsigned width = 0, height = 0;
    unsigned hold = 0;
    int keep = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--alloc") && i + 1 < argc) alloc_mb = strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--image") && i + 1 < argc) sscanf(argv[++i], "%ux%u", &width, &height);
        else if (!strcmp(argv[i], "--repeat") && i + 1 < argc) repeat = strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--hold") && i + 1 < argc) hold = (unsigned)strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "--keep")) keep = 1;
        else {
            fprintf(stderr, "usage: %s [--alloc MB] [--image WxH] [--repeat N] [--keep] [--hold S]\n", argv[0]);
            return 2;
        }
    }
    if (repeat == 0) repeat = 1;
    if (repeat > 64) repeat = 64;

    if (!load_vulkan()) {
        printf("pid=%d loader=missing\n", (int)getpid());
        return 0;
    }
    PFN_vkCreateInstance create_instance = (PFN_vkCreateInstance)gipa(NULL, "vkCreateInstance");
    if (!create_instance) {
        printf("pid=%d loader=incomplete\n", (int)getpid());
        return 0;
    }

    VkApplicationInfo app = {VK_STRUCTURE_TYPE_APPLICATION_INFO, NULL, "mb-mem-probe", 1, "mb-mem-probe", 1, VK_API_VERSION_1_3};
    VkInstanceCreateInfo ici = {VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, NULL, 0, &app, 0, NULL, 0, NULL};
    VkInstance inst = VK_NULL_HANDLE;
    VkResult r = create_instance(&ici, NULL, &inst);
    printf("pid=%d create_instance result=%d\n", (int)getpid(), (int)r);
    if (r != VK_SUCCESS || !inst) return 0;

    PFN_vkEnumeratePhysicalDevices enum_pd = (PFN_vkEnumeratePhysicalDevices)gipa(inst, "vkEnumeratePhysicalDevices");
    PFN_vkGetPhysicalDeviceQueueFamilyProperties get_qf =
        (PFN_vkGetPhysicalDeviceQueueFamilyProperties)gipa(inst, "vkGetPhysicalDeviceQueueFamilyProperties");
    PFN_vkGetPhysicalDeviceMemoryProperties get_mp =
        (PFN_vkGetPhysicalDeviceMemoryProperties)gipa(inst, "vkGetPhysicalDeviceMemoryProperties");
    PFN_vkDestroyInstance destroy_instance = (PFN_vkDestroyInstance)gipa(inst, "vkDestroyInstance");
    if (!enum_pd || !get_qf || !get_mp) {
        printf("pid=%d loader=incomplete\n", (int)getpid());
        if (destroy_instance) destroy_instance(inst, NULL);
        return 0;
    }

    uint32_t n = 0;
    r = enum_pd(inst, &n, NULL);
    VkPhysicalDevice pds[8];
    if (r != VK_SUCCESS || n == 0) {
        printf("pid=%d enumerate_devices result=%d count=%u\n", (int)getpid(), (int)r, n);
        if (destroy_instance) destroy_instance(inst, NULL);
        return 0;
    }
    if (n > 8) n = 8;
    r = enum_pd(inst, &n, pds);
    if (r != VK_SUCCESS || n == 0) {
        printf("pid=%d enumerate_devices(result) result=%d\n", (int)getpid(), (int)r);
        if (destroy_instance) destroy_instance(inst, NULL);
        return 0;
    }

    uint32_t qn = 0;
    get_qf(pds[0], &qn, NULL);
    VkQueueFamilyProperties qs[32];
    if (qn > 32) qn = 32;
    get_qf(pds[0], &qn, qs);
    int q = -1;
    for (uint32_t i = 0; i < qn && q < 0; i++)
        if ((qs[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) || (qs[i].queueFlags & VK_QUEUE_COMPUTE_BIT)) q = (int)i;
    if (q < 0) {
        printf("pid=%d no_queue_family\n", (int)getpid());
        if (destroy_instance) destroy_instance(inst, NULL);
        return 0;
    }
    VkPhysicalDeviceMemoryProperties mp;
    get_mp(pds[0], &mp);

    float prio = 1.0f;
    VkDeviceQueueCreateInfo qci = {VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, NULL, 0, (uint32_t)q, 1, &prio};
    VkDeviceCreateInfo dci = {VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, NULL, 0, 1, &qci, 0, NULL, 0, NULL, NULL};
    PFN_vkCreateDevice create_device = (PFN_vkCreateDevice)gipa(inst, "vkCreateDevice");
    PFN_vkDestroyDevice destroy_device = (PFN_vkDestroyDevice)gipa(inst, "vkDestroyDevice");
    VkDevice dev = VK_NULL_HANDLE;
    r = create_device ? create_device(pds[0], &dci, NULL, &dev) : VK_ERROR_INITIALIZATION_FAILED;
    printf("pid=%d create_device result=%d\n", (int)getpid(), (int)r);
    if (r != VK_SUCCESS || !dev) {
        if (destroy_instance) destroy_instance(inst, NULL);
        return 0;
    }
    PFN_vkGetDeviceProcAddr gdpa = (PFN_vkGetDeviceProcAddr)gipa(inst, "vkGetDeviceProcAddr");
#define LOAD_DEV(name) PFN_##name name = gdpa ? (PFN_##name)gdpa(dev, "vk" #name) : NULL
    LOAD_DEV(vkCreateBuffer);
    LOAD_DEV(vkDestroyBuffer);
    LOAD_DEV(vkGetBufferMemoryRequirements);
    LOAD_DEV(vkCreateImage);
    LOAD_DEV(vkDestroyImage);
    LOAD_DEV(vkGetImageMemoryRequirements);
    LOAD_DEV(vkAllocateMemory);
    LOAD_DEV(vkFreeMemory);
    LOAD_DEV(vkBindBufferMemory);
    LOAD_DEV(vkBindImageMemory);
#undef LOAD_DEV

    VkBuffer bufs[64];
    VkImage images[64];
    VkDeviceMemory mems[64];
    int nkeep = 0;
    for (unsigned long i = 0; i < repeat; i++) {
        if (width && height) {
            VkImageCreateInfo ci = {VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
                                    NULL,
                                    0,
                                    VK_IMAGE_TYPE_2D,
                                    VK_FORMAT_R8G8B8A8_UNORM,
                                    {width, height, 1},
                                    1,
                                    1,
                                    VK_SAMPLE_COUNT_1_BIT,
                                    VK_IMAGE_TILING_OPTIMAL,
                                    VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT,
                                    VK_SHARING_MODE_EXCLUSIVE,
                                    0,
                                    NULL,
                                    VK_IMAGE_LAYOUT_UNDEFINED};
            VkImage img = VK_NULL_HANDLE;
            r = vkCreateImage ? vkCreateImage(dev, &ci, NULL, &img) : VK_ERROR_INITIALIZATION_FAILED;
            printf("image %ux%u pid=%d result=%d\n", width, height, (int)getpid(), (int)r);
            if (r != VK_SUCCESS) continue;
            VkMemoryRequirements req;
            vkGetImageMemoryRequirements(dev, img, &req);
            uint32_t type = pick_type(&mp, req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
            VkMemoryAllocateInfo ai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL, req.size, type};
            VkDeviceMemory mem = VK_NULL_HANDLE;
            r = type != UINT32_MAX && vkAllocateMemory ? vkAllocateMemory(dev, &ai, NULL, &mem) : VK_ERROR_OUT_OF_DEVICE_MEMORY;
            printf("image_alloc pid=%d bytes=%llu type=%u result=%d\n", (int)getpid(), (unsigned long long)req.size, type, (int)r);
            if (r == VK_SUCCESS) {
                vkBindImageMemory(dev, img, mem, 0);
                if (keep && nkeep < 64) {
                    images[nkeep] = img;
                    mems[nkeep] = mem;
                    nkeep++;
                    continue;
                }
                vkFreeMemory(dev, mem, NULL);
            }
            vkDestroyImage(dev, img, NULL);
        } else {
            VkBufferCreateInfo ci = {VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, NULL, 0, (VkDeviceSize)alloc_mb << 20,
                                     VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT,
                                     VK_SHARING_MODE_EXCLUSIVE, 0, NULL};
            VkBuffer buf = VK_NULL_HANDLE;
            r = vkCreateBuffer ? vkCreateBuffer(dev, &ci, NULL, &buf) : VK_ERROR_INITIALIZATION_FAILED;
            if (r != VK_SUCCESS) {
                printf("alloc %lu create_buffer result=%d\n", i, (int)r);
                continue;
            }
            VkMemoryRequirements req;
            vkGetBufferMemoryRequirements(dev, buf, &req);
            uint32_t type = pick_type(&mp, req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
            VkMemoryAllocateInfo ai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL, req.size, type};
            VkDeviceMemory mem = VK_NULL_HANDLE;
            r = type != UINT32_MAX && vkAllocateMemory ? vkAllocateMemory(dev, &ai, NULL, &mem) : VK_ERROR_OUT_OF_DEVICE_MEMORY;
            printf("alloc %lu pid=%d bytes=%llu type=%u result=%d\n", i, (int)getpid(), (unsigned long long)req.size, type, (int)r);
            if (r == VK_SUCCESS) {
                vkBindBufferMemory(dev, buf, mem, 0);
                if (keep && nkeep < 64) {
                    bufs[nkeep] = buf;
                    mems[nkeep] = mem;
                    nkeep++;
                    continue;
                }
                vkFreeMemory(dev, mem, NULL);
            }
            vkDestroyBuffer(dev, buf, NULL);
        }
    }
    if (hold) sleep(hold);
    for (int i = 0; i < nkeep; i++) {
        vkFreeMemory(dev, mems[i], NULL);
        if (width && height) vkDestroyImage(dev, images[i], NULL);
        else vkDestroyBuffer(dev, bufs[i], NULL);
    }
    if (destroy_device) destroy_device(dev, NULL);
    if (destroy_instance) destroy_instance(inst, NULL);
    return 0;
}
