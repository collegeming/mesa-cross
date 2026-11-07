/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Copyright (C) 2026 ASEbrahim and MaliBridge contributors */
/* Build-time smoke for the client library: it loads, its ICD entry point resolves, and creating an
 * instance does not crash. No GPU or bridge server exists in CI, so an error result is recorded as
 * such: the A/B comparison is that baseline and candidate answer the same. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <vulkan/vulkan.h>

int main(int argc, char **argv)
{
    if (argc != 2) return 2;
    void *lib = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!lib) {
        printf("dlopen=FAIL\n");
        return 1;
    }
    PFN_vkGetInstanceProcAddr gipa = (PFN_vkGetInstanceProcAddr)dlsym(lib, "vk_icdGetInstanceProcAddr");
    if (!gipa) gipa = (PFN_vkGetInstanceProcAddr)dlsym(lib, "vkGetInstanceProcAddr");
    PFN_vkCreateInstance create = gipa ? (PFN_vkCreateInstance)gipa(NULL, "vkCreateInstance") : NULL;
    printf("gipa=%s createInstance=%s\n", gipa ? "OK" : "NULL", create ? "OK" : "NULL");
    if (!create) return 1;
    VkApplicationInfo app = {VK_STRUCTURE_TYPE_APPLICATION_INFO};
    app.apiVersion = VK_API_VERSION_1_3;
    VkInstanceCreateInfo ci = {VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    ci.pApplicationInfo = &app;
    VkInstance instance = VK_NULL_HANDLE;
    VkResult r = create(&ci, NULL, &instance);
    printf("vkCreateInstance=%d\n", (int)r);
    if (r != VK_SUCCESS) return 0;
    if (!instance) {
        printf("instance=NULL\n");
        return 0;
    }
    PFN_vkEnumeratePhysicalDevices devices = (PFN_vkEnumeratePhysicalDevices)gipa(instance, "vkEnumeratePhysicalDevices");
    if (!devices) {
        printf("vkEnumeratePhysicalDevices=NULL\n");
    } else {
        uint32_t n = 0;
        VkResult pr = devices(instance, &n, NULL);
        printf("vkEnumeratePhysicalDevices=%d count=%u\n", (int)pr, n);
    }
    PFN_vkDestroyInstance destroy = (PFN_vkDestroyInstance)gipa(instance, "vkDestroyInstance");
    if (destroy) destroy(instance, NULL);
    return 0;
}
